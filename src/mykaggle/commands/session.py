import datetime
import json
import os
import re
import sys
import time
import uuid
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import requests
import typer
from rich.console import Console
from rich.table import Table
from typing_extensions import Annotated

from mykaggle.common import make_runtime, resolve_session, state
from mykaggle.state import SessionState
from mykaggle.urls import redact, rest_url, split_server_url
from mykaggle.utils import print_output

_console = Console()

_MACHINE_SHAPES = {
    "t4": "NvidiaTeslaT4",
    "2xt4": "NvidiaTeslaT4",
    "nvidiateslat4": "NvidiaTeslaT4",
    "p100": "NvidiaTeslaP100",
    "nvidiateslap100": "NvidiaTeslaP100",
    "l4": "NvidiaL4",
    "4xl4": "NvidiaL4",
    "nvidial4": "NvidiaL4",
    "tpu": "TpuV5E8",
    "v5e": "TpuV5E8",
    "tpuv5e8": "TpuV5E8",
    "v3-8": "Tpu1VmV38",
    "tpu1vmv38": "Tpu1VmV38",
    "cpu": "",
    "none": "",
}


def _extract_session_id_from_url(url: str) -> Optional[int]:
    m = re.search(r"/k/(\d+)/", url)
    return int(m.group(1)) if m else None


def _load_kaggle_auth() -> Tuple[dict, Optional[Tuple[str, str]], Optional[str]]:
    """Return (headers, basic_auth, username) from Kaggle credentials."""
    api_tok = os.getenv("KAGGLE_API_TOKEN")
    if api_tok:
        return {"Authorization": f"Bearer {api_tok}"}, None, os.getenv("KAGGLE_USERNAME")

    # Check ~/.kaggle/credentials.json (with fallback to /home/<user>/.kaggle if HOME is overridden)
    cred_candidates = [
        os.path.expanduser("~/.kaggle/credentials.json"),
        "/home/carlesoctav/.kaggle/credentials.json",
    ]
    for cred_path in cred_candidates:
        if os.path.exists(cred_path):
            try:
                with open(cred_path) as f:
                    data = json.load(f)
                access_token = data.get("access_token")
                refresh_token = data.get("refresh_token")
                exp_str = data.get("access_token_expiration")
                username = data.get("username")
                needs_refresh = True
                if access_token and exp_str:
                    exp_dt = datetime.datetime.fromisoformat(exp_str)
                    now_utc = datetime.datetime.now(datetime.timezone.utc)
                    if exp_dt > now_utc + datetime.timedelta(minutes=30):
                        needs_refresh = False
                if needs_refresh and refresh_token:
                    r = requests.post(
                        "https://api.kaggle.com/v1/users.AccountService/GenerateAccessToken",
                        json={
                            "refreshToken": refresh_token,
                            "apiVersion": "API_VERSION_V1",
                            "expirationDuration": "43200s",
                        },
                        timeout=15,
                    )
                    r.raise_for_status()
                    tok_data = r.json()
                    access_token = tok_data["token"]
                    expires_in = int(tok_data.get("expiresIn", 43200))
                    new_exp = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
                        seconds=expires_in
                    )
                    data["access_token"] = access_token
                    data["access_token_expiration"] = new_exp.isoformat()
                    try:
                        with open(cred_path, "w") as wf:
                            json.dump(data, wf, indent=2)
                    except OSError:
                        pass
                if access_token:
                    return {"Authorization": f"Bearer {access_token}"}, None, username
            except Exception:
                pass

    # Fallback to ~/.kaggle/kaggle.json
    key_candidates = [
        os.path.expanduser("~/.kaggle/kaggle.json"),
        "/home/carlesoctav/.kaggle/kaggle.json",
    ]
    for key_path in key_candidates:
        if os.path.exists(key_path):
            try:
                with open(key_path) as f:
                    kd = json.load(f)
                return {}, (kd["username"], kd["key"]), kd.get("username")
            except Exception:
                pass

    if os.getenv("KAGGLE_USERNAME") and os.getenv("KAGGLE_KEY"):
        u = os.getenv("KAGGLE_USERNAME")
        return {}, (u, os.getenv("KAGGLE_KEY")), u

    typer.echo(
        "[mykaggle] Kaggle credentials not found (~/.kaggle/credentials.json or ~/.kaggle/kaggle.json).",
        err=True,
    )
    raise typer.Exit(1)


def _kaggle_rpc(service: str, method: str, payload: dict) -> dict:
    headers, basic_auth, _ = _load_kaggle_auth()
    req_headers = {"User-Agent": "kaggle-api/v1.7.0", "Content-Type": "application/json"}
    req_headers.update(headers)
    url = f"https://api.kaggle.com/v1/{service}/{method}"
    r = requests.post(url, headers=req_headers, auth=basic_auth, json=payload, timeout=30)
    try:
        data = r.json()
        if isinstance(data, dict) and data.get("code", 0) >= 400:
            raise RuntimeError(data.get("message") or r.text)
    except ValueError:
        data = {}
    r.raise_for_status()
    return data


def connect_to(
    url: str,
    kernel_slug: Optional[str] = None,
    kernel_session_id: Optional[int] = None,
) -> SessionState:
    """Connect to a compatible URL, attach to primary kernel, persist the session."""
    url = url.strip()
    base, token = split_server_url(url)
    host = urlparse(base).netloc or base

    try:
        kernels = requests.get(
            rest_url(base, token, "/api/kernels"), timeout=15
        ).json()
    except Exception as e:
        typer.echo(f"[mykaggle] Could not reach server: {redact(str(e))}", err=True)
        raise typer.Exit(1)

    if kernels:
        kernel = kernels[0]
        kernel_id = kernel["id"]
        typer.echo(
            f"[mykaggle] Attached to existing kernel {kernel_id} "
            f"({kernel.get('name')}, {kernel.get('execution_state')})."
        )
    else:
        try:
            r = requests.post(
                rest_url(base, token, "/api/kernels"),
                json={"name": "python3"},
                timeout=30,
            )
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            typer.echo(f"[mykaggle] Could not start kernel: {redact(str(e))}", err=True)
            raise typer.Exit(1)
        kernel_id = r.json()["id"]
        typer.echo(f"[mykaggle] Started new kernel {kernel_id}.")

    # Preserve any existing named kernels if reconnecting to same host
    prev = state.store.load()
    kernels_dict = {}
    if prev and prev.url == url:
        kernels_dict = prev.kernels
    if not kernel_slug and prev:
        kernel_slug = prev.kernel_slug
    if kernel_session_id is None:
        kernel_session_id = _extract_session_id_from_url(base) or _extract_session_id_from_url(url)

    session = SessionState(
        url=url,
        kernel_id=kernel_id,
        session_id=uuid.uuid4().hex,
        connected_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        kernel_slug=kernel_slug,
        kernel_session_id=kernel_session_id,
        kernels=kernels_dict,
    )
    state.store.save(session)
    typer.echo(f"[mykaggle] Connected to {host} (state saved).")
    return session


def connect_command(
    url: Annotated[str, typer.Argument(help="Compatible server URL")],
):
    """Connect to a Kaggle server (replaces the current session)."""
    connect_to(url)


def _create_kernel_entity(
    slug: str,
    title: Optional[str] = None,
    kernel_type: str = "notebook",
    language: str = "python",
    is_private: bool = True,
    enable_internet: bool = False,
    machine_shape: Optional[str] = None,
    competition_sources: Optional[List[str]] = None,
    dataset_sources: Optional[List[str]] = None,
    model_sources: Optional[List[str]] = None,
    source_text: Optional[str] = None,
) -> dict:
    parts = slug.split("/", 1)
    kernel_slug = parts[1] if len(parts) > 1 else parts[0]
    effective_title = (title or kernel_slug.replace("-", " ").title()).strip()
    if len(effective_title) < 5:
        effective_title = f"{effective_title} kernel"

    if source_text is None:
        if kernel_type == "notebook":
            nb = {
                "cells": [
                    {
                        "cell_type": "code",
                        "execution_count": None,
                        "metadata": {},
                        "outputs": [],
                        "source": ["# Initialized by mykaggle\n"],
                    }
                ],
                "metadata": {
                    "kernelspec": {
                        "display_name": "Python 3",
                        "language": "python",
                        "name": "python3",
                    },
                    "language_info": {"name": "python"},
                },
                "nbformat": 4,
                "nbformat_minor": 4,
            }
            source_text = json.dumps(nb, indent=1)
        else:
            source_text = "# Initialized by mykaggle\n"

    resolved_shape = (
        _MACHINE_SHAPES.get(machine_shape.lower().strip(), machine_shape.strip())
        if machine_shape
        else ""
    )

    save_payload = {
        "slug": slug,
        "newTitle": effective_title,
        "text": source_text,
        "language": language,
        "kernelType": kernel_type,
        "isPrivate": is_private,
        "enableGpu": bool(resolved_shape and resolved_shape.startswith("Nvidia")),
        "enableTpu": bool(resolved_shape and resolved_shape.startswith("Tpu")),
        "enableInternet": enable_internet,
        "competitionDataSources": competition_sources or [],
        "datasetDataSources": dataset_sources or [],
        "modelDataSources": model_sources or [],
        "categoryIds": [],
        "kernelExecutionType": "QUICK_SAVE",
    }
    if resolved_shape:
        save_payload["machineShape"] = resolved_shape

    return _kaggle_rpc("kernels.KernelsApiService", "SaveKernel", save_payload)


def create_command(
    name: Annotated[
        str,
        typer.Argument(help="Kernel slug 'owner/kernel-slug' or 'kernel-slug'"),
    ],
    title: Annotated[
        Optional[str],
        typer.Option("-t", "--title", help="Kernel title (min 5 characters)"),
    ] = None,
    machine: Annotated[
        Optional[str],
        typer.Option(
            "-m",
            "--machine",
            help="Accelerator: t4, p100, l4 (4xl4), tpu (v5e), v3-8, cpu, or raw shape name",
        ),
    ] = None,
    kernel_type: Annotated[
        str,
        typer.Option("--type", help="Kernel type: 'notebook' or 'script'"),
    ] = "notebook",
    internet: Annotated[
        bool,
        typer.Option("--internet/--no-internet", help="Enable internet access"),
    ] = False,
    private: Annotated[
        bool,
        typer.Option("--private/--public", help="Whether the kernel is private"),
    ] = True,
    competition: Annotated[
        Optional[List[str]],
        typer.Option("-c", "--competition", help="Competition slug to attach (e.g. gemma-4-developer-agent)"),
    ] = None,
    dataset: Annotated[
        Optional[List[str]],
        typer.Option("-d", "--dataset", help="Dataset slug 'owner/slug' to attach"),
    ] = None,
    model: Annotated[
        Optional[List[str]],
        typer.Option("--model", help="Model slug 'owner/model/framework/variation' or with version to attach"),
    ] = None,
    file: Annotated[
        Optional[str],
        typer.Option("-f", "--file", help="Local .py or .ipynb file to initialize code with"),
    ] = None,
    start: Annotated[
        bool,
        typer.Option("-s", "--start", help="Immediately start an interactive VM session and connect to it"),
    ] = False,
):
    """Create a brand new Kaggle kernel entity."""
    _, _, authed_user = _load_kaggle_auth()
    slug = name
    if "/" not in slug:
        user = authed_user or "carlesoctav"
        slug = f"{user}/{slug}"

    source_text = None
    if file:
        if not os.path.exists(file):
            typer.echo(f"[mykaggle] File not found: {file}", err=True)
            raise typer.Exit(1)
        with open(file, "r", encoding="utf-8") as rf:
            source_text = rf.read()
        if file.endswith(".ipynb"):
            kernel_type = "notebook"
        elif file.endswith(".py") and kernel_type != "notebook":
            kernel_type = "script"

    try:
        resp = _create_kernel_entity(
            slug=slug,
            title=title,
            kernel_type=kernel_type,
            is_private=private,
            enable_internet=internet,
            machine_shape=machine,
            competition_sources=competition,
            dataset_sources=dataset,
            model_sources=model,
            source_text=source_text,
        )
    except Exception as e:
        typer.echo(f"[mykaggle] Failed to create kernel '{slug}': {e}", err=True)
        raise typer.Exit(1)

    if resp.get("error"):
        typer.echo(f"[mykaggle] Error: {resp['error']}", err=True)
        raise typer.Exit(1)

    url = resp.get("url") or f"https://www.kaggle.com/code/{slug}"
    typer.echo(f"[mykaggle] Successfully created kernel '{slug}': {url}")

    if start:
        start_command(slug=slug, machine=machine, internet=internet)


def delete_command(
    slug: Annotated[
        str,
        typer.Argument(help="Kernel slug 'owner/kernel-slug' or 'kernel-slug' to delete"),
    ],
    yes: Annotated[
        bool,
        typer.Option("-y", "--yes", help="Skip confirmation prompt"),
    ] = False,
):
    """Delete a kernel from Kaggle."""
    _, _, authed_user = _load_kaggle_auth()
    if "/" not in slug:
        user = authed_user or "carlesoctav"
        slug = f"{user}/{slug}"
    owner, kernel_slug = slug.split("/", 1)

    if not yes:
        confirm = typer.confirm(f"Are you sure you want to delete kernel '{slug}' from Kaggle?")
        if not confirm:
            typer.echo("[mykaggle] Aborted.")
            raise typer.Exit(0)

    try:
        res = _kaggle_rpc(
            "kernels.KernelsApiService",
            "DeleteKernel",
            {"userName": owner, "kernelSlug": kernel_slug},
        )
        if res.get("errorMessage"):
            typer.echo(f"[mykaggle] Delete error: {res['errorMessage']}", err=True)
            raise typer.Exit(1)
        typer.echo(f"[mykaggle] Deleted kernel '{slug}' from Kaggle.")
    except Exception as e:
        typer.echo(f"[mykaggle] Failed to delete kernel '{slug}': {e}", err=True)
        raise typer.Exit(1)


def start_command(
    slug: Annotated[
        Optional[str],
        typer.Argument(
            help="Kernel slug 'owner/kernel-slug' (defaults to last connected kernel or 'mykaggle-batch-test')"
        ),
    ] = None,
    machine: Annotated[
        Optional[str],
        typer.Option(
            "-m",
            "--machine",
            help="Accelerator / machine shape: t4, p100, l4 (4xl4), tpu (v5e), v3-8, cpu, or raw shape name",
        ),
    ] = None,
    internet: Annotated[
        Optional[bool],
        typer.Option(
            "--internet/--no-internet",
            help="Override internet access for this session (defaults to kernel metadata)",
        ),
    ] = None,
    keep_previous: Annotated[
        bool,
        typer.Option(
            "--keep-previous",
            help="Do not cancel the previously connected Kaggle VM session before starting",
        ),
    ] = False,
    timeout: Annotated[
        float,
        typer.Option("--timeout", help="Timeout in seconds to wait for VM session to boot"),
    ] = 180.0,
):
    """Start (or restart) an interactive Kaggle VM session and connect to it."""
    prev = state.store.load()
    _, _, authed_user = _load_kaggle_auth()

    if not slug:
        slug = (prev.kernel_slug if prev and prev.kernel_slug else None) or (
            f"{authed_user}/mykaggle-batch-test" if authed_user else "carlesoctav/mykaggle-batch-test"
        )
    elif "/" not in slug:
        user = authed_user or "carlesoctav"
        slug = f"{user}/{slug}"

    owner, kernel_slug = slug.split("/", 1)

    # Cancel previous active VM session unless --keep-previous
    if not keep_previous and prev:
        prev_sid = prev.kernel_session_id or _extract_session_id_from_url(prev.url)
        if prev_sid:
            try:
                _kaggle_rpc(
                    "kernels.KernelsApiService",
                    "CancelKernelSession",
                    {"kernelSessionId": prev_sid},
                )
                typer.echo(f"[mykaggle] Canceled previous VM session {prev_sid}.")
                time.sleep(1.5)
            except Exception as e:
                typer.echo(f"[mykaggle] Note: could not cancel session {prev_sid}: {e}", err=True)

    # Fetch kernel metadata for defaults
    default_shape = ""
    default_internet = False
    try:
        kinfo = _kaggle_rpc(
            "kernels.KernelsApiService",
            "GetKernel",
            {"userName": owner, "kernelSlug": kernel_slug},
        )
        meta = kinfo.get("metadata") or {}
        default_shape = meta.get("machineShape") or ""
        default_internet = bool(meta.get("enableInternet", False))
    except Exception:
        # Kernel doesn't exist yet on Kaggle! Auto-create it so CreateKernelSession succeeds.
        resolved_shape = (
            _MACHINE_SHAPES.get(machine.lower().strip(), machine.strip())
            if machine
            else ""
        )
        resolved_internet = False if internet is None else internet
        typer.echo(f"[mykaggle] Kernel '{slug}' not found on Kaggle; initializing new kernel...")
        _create_kernel_entity(
            slug=slug,
            kernel_type="notebook",
            enable_internet=resolved_internet,
            machine_shape=resolved_shape,
        )
        typer.echo(f"[mykaggle] Initialized kernel '{slug}'.")
        meta = {}

    if machine is not None:
        resolved_shape = _MACHINE_SHAPES.get(machine.lower().strip(), machine.strip())
    else:
        resolved_shape = default_shape

    resolved_internet = default_internet if internet is None else internet

    req_payload = {
        "slug": slug,
        "language": "python",
        "kernelType": "notebook",
        "enableInternet": resolved_internet,
    }
    if resolved_shape:
        req_payload["machineShape"] = resolved_shape

    shape_label = resolved_shape or "CPU"
    typer.echo(
        f"[mykaggle] Starting Kaggle VM session for '{slug}' "
        f"(machine={shape_label}, internet={resolved_internet})..."
    )

    try:
        op = _kaggle_rpc("kernels.KernelsApiService", "CreateKernelSession", req_payload)
    except Exception as e:
        typer.echo(f"[mykaggle] Failed to start session: {e}", err=True)
        raise typer.Exit(1)

    op_name = op.get("name")
    ks_id = (op.get("metadata") or {}).get("kernelSessionId")
    typer.echo(f"[mykaggle] Provisioning VM session {ks_id} ({op_name})...")

    deadline = time.time() + timeout
    while time.time() < deadline:
        if op.get("done"):
            break
        time.sleep(2.0)
        op = _kaggle_rpc("common.OperationsService", "GetOperation", {"name": op_name})

    if not op.get("done"):
        typer.echo(f"[mykaggle] Timed out waiting for session {ks_id} to start.", err=True)
        raise typer.Exit(1)

    if op.get("error"):
        err = op["error"]
        typer.echo(
            f"[mykaggle] Session failed to start ({err.get('code')}): {err.get('message')}",
            err=True,
        )
        raise typer.Exit(1)

    resp = op.get("response") or {}
    jupyter_url = resp.get("jupyterUrl") or resp.get("tokenizedJupyterUrl")
    if not jupyter_url:
        typer.echo(f"[mykaggle] Unexpected operation response: {op}", err=True)
        raise typer.Exit(1)

    connect_to(jupyter_url, kernel_slug=slug, kernel_session_id=ks_id)


def stop_command(
    session_id: Annotated[
        Optional[int],
        typer.Argument(help="Kaggle kernel session ID to cancel (defaults to active session)"),
    ] = None,
):
    """Cancel (shut down) the active Kaggle VM session."""
    prev = state.store.load()
    if session_id is None and prev:
        session_id = prev.kernel_session_id or _extract_session_id_from_url(prev.url)

    if not session_id:
        typer.echo(
            "[mykaggle] No active kernel_session_id found in state; pass SESSION_ID explicitly.",
            err=True,
        )
        raise typer.Exit(1)

    try:
        res = _kaggle_rpc(
            "kernels.KernelsApiService",
            "CancelKernelSession",
            {"kernelSessionId": session_id},
        )
        err_msg = res.get("errorMessage")
        if err_msg:
            typer.echo(f"[mykaggle] Cancel returned error: {err_msg}", err=True)
            raise typer.Exit(1)
        typer.echo(f"[mykaggle] Canceled Kaggle VM session {session_id}.")
    except Exception as e:
        typer.echo(f"[mykaggle] Failed to cancel session {session_id}: {e}", err=True)
        raise typer.Exit(1)


def _build_attach_payload(
    ref: str,
    version: Optional[int] = None,
    as_kernel: bool = False,
    as_competition: bool = False,
) -> dict:
    """Parse a Kaggle datasource handle into an AttachDatasourceUsingJwtRequest payload.

    Supported formats:
      - owner/dataset-slug                  (dataset)
      - owner/dataset-slug/versions/3       (versioned dataset)
      - owner/model/framework/variation     (model)
      - owner/model/framework/variation/2   (versioned model)
      - competition-slug                    (competition, or with --competition)
      - owner/kernel-slug --kernel          (notebook/kernel output)
    """
    parts = [p for p in ref.strip("/").split("/") if p]

    if as_competition or len(parts) == 1:
        return {"competitionRef": {"CompetitionSlug": parts[0]}}

    if len(parts) >= 4 and parts[2] == "versions":
        # owner/slug/versions/N
        owner, slug, _, ver_str = parts[:4]
        ver = str(version) if version is not None else ver_str
        if as_kernel:
            return {"kernelRef": {"OwnerSlug": owner, "KernelSlug": slug, "VersionNumber": ver}}
        return {"datasetRef": {"OwnerSlug": owner, "DatasetSlug": slug, "VersionNumber": ver}}

    if len(parts) == 2:
        owner, slug = parts
        ref_dict = {"OwnerSlug": owner, "KernelSlug" if as_kernel else "DatasetSlug": slug}
        if version is not None:
            ref_dict["VersionNumber"] = str(version)
        return {"kernelRef" if as_kernel else "datasetRef": ref_dict}

    if len(parts) in (4, 5):
        owner, model_slug, framework, variation = parts[:4]
        model_ref = {
            "OwnerSlug": owner,
            "ModelSlug": model_slug,
            "Framework": framework,
            "InstanceSlug": variation,
        }
        if version is not None:
            model_ref["VersionNumber"] = str(version)
        elif len(parts) == 5:
            model_ref["VersionNumber"] = parts[4]
        return {"modelRef": model_ref}

    raise typer.BadParameter(
        f"Unrecognized datasource format '{ref}'. Expected 'owner/dataset', "
        "'owner/dataset/versions/N', 'owner/model/framework/variation[/N]', or 'competition-slug'."
    )


def attach_command(
    datasource: Annotated[
        str,
        typer.Argument(
            help="Datasource handle: 'owner/dataset', 'owner/dataset/versions/N', 'owner/model/framework/variation', or 'competition-slug'"
        ),
    ],
    version: Annotated[
        Optional[int],
        typer.Option("-v", "--version", help="Specific version number to attach/refresh"),
    ] = None,
    kernel: Annotated[
        bool,
        typer.Option("-k", "--kernel", help="Treat 'owner/slug' as a kernel/notebook output instead of a dataset"),
    ] = False,
    competition: Annotated[
        bool,
        typer.Option("-c", "--competition", help="Treat handle as a competition slug"),
    ] = False,
    timeout: Annotated[
        Optional[float],
        typer.Option("--timeout", help="Timeout in seconds to wait for mount"),
    ] = 60.0,
):
    """Dynamically attach or refresh a dataset, model, kernel, or competition in /kaggle/input."""
    payload = _build_attach_payload(
        datasource, version=version, as_kernel=kernel, as_competition=competition
    )
    payload_json = json.dumps(payload)

    code = f"""
import json, os, sys, time, requests

_endpoint = os.getenv("KAGGLE_DATA_PROXY_URL", "https://dp.kaggle.net").rstrip("/")
_jwt = os.getenv("KAGGLE_USER_SECRETS_TOKEN")
_proxy_tok = os.getenv("KAGGLE_DATA_PROXY_TOKEN")

if not _jwt or not _proxy_tok:
    print("[mykaggle] Error: KAGGLE_USER_SECRETS_TOKEN / KAGGLE_DATA_PROXY_TOKEN missing in container.", file=sys.stderr)
    sys.exit(1)

_url = f"{{_endpoint}}/kaggle-jwt-handler/AttachDatasourceUsingJwtRequest"
_headers = {{
    "Content-Type": "application/json",
    "X-Kaggle-Authorization": f"Bearer {{_jwt}}",
    "X-KAGGLE-PROXY-DATA": _proxy_tok,
}}
_payload = {payload_json!r}

_resp = requests.post(_url, headers=_headers, data=_payload.encode("utf-8"), timeout=(5, 30))
_resp.raise_for_status()
_data = _resp.json()
if not _data.get("wasSuccessful"):
    print(f"[mykaggle] Attach failed: {{_resp.text}}", file=sys.stderr)
    sys.exit(1)

_res = _data.get("result", {{}})
_mount_slug = _res.get("mountSlug")
_ver = _res.get("versionNumber")
_base_mount = os.getenv("KAGGLE_CACHE_MOUNT_FOLDER", "/kaggle/input")
_mount_path = f"{{_base_mount}}/{{_mount_slug}}"

print(f"[mykaggle] Attached! Waiting for mount at {{_mount_path}} (version={{_ver}})...", flush=True)
_deadline = time.time() + 45
while not os.path.exists(_mount_path) and time.time() < _deadline:
    time.sleep(1)

if os.path.exists(_mount_path):
    _files = os.listdir(_mount_path)
    print(f"[mykaggle] Mounted at: {{_mount_path}} ({{len(_files)}} items: {{_files[:10]}})")
else:
    print(f"[mykaggle] Warning: RPC succeeded (mountSlug={{_mount_slug}}), but {{_mount_path}} not yet visible.", file=sys.stderr)
"""

    s = resolve_session()
    runtime = make_runtime(s)
    try:
        runtime.execute_code(
            code,
            output_hook=lambda o: print_output(o, _console),
            timeout=timeout,
        )
    finally:
        runtime.stop(shutdown_kernel=False)


def _AdaptSourceToKernelType(source: str, kernel_type: str) -> str:
    """Ensure source text matches Kaggle's expected kernelType ('script' vs 'notebook')."""
    stripped = source.lstrip()
    is_ipynb = stripped.startswith("{") and '"cells"' in stripped[:2000]
    if kernel_type == "notebook" and not is_ipynb:
        nb = {
            "cells": [
                {
                    "cell_type": "code",
                    "execution_count": None,
                    "metadata": {},
                    "outputs": [],
                    "source": source.splitlines(keepends=True),
                }
            ],
            "metadata": {
                "kernelspec": {
                    "display_name": "Python 3",
                    "language": "python",
                    "name": "python3",
                },
                "language_info": {"name": "python"},
            },
            "nbformat": 4,
            "nbformat_minor": 4,
        }
        return json.dumps(nb, indent=1)
    if kernel_type == "script" and is_ipynb:
        try:
            nb = json.loads(source)
            blocks = []
            for cell in nb.get("cells", []):
                if cell.get("cell_type") == "code":
                    src = cell.get("source", "")
                    if isinstance(src, list):
                        src = "".join(src)
                    blocks.append(src)
            return "\n\n".join(blocks) + "\n"
        except Exception:
            return source
    return source


def commit_command(
    slug: Annotated[
        Optional[str],
        typer.Argument(
            help="Kernel slug 'owner/kernel-slug' (defaults to last connected kernel or 'mykaggle-batch-test')"
        ),
    ] = None,
    file: Annotated[
        Optional[str],
        typer.Option(
            "-f",
            "--file",
            help="Local .py or .ipynb file to commit & run (defaults to kernel's existing saved code)",
        ),
    ] = None,
    cmd: Annotated[
        Optional[str],
        typer.Option(
            "-c",
            "--cmd",
            help="Shell command to execute in the batch run (wraps command in a Python script)",
        ),
    ] = None,
    machine: Annotated[
        Optional[str],
        typer.Option(
            "-m",
            "--machine",
            help="Accelerator / machine shape: t4, p100, l4 (4xl4), tpu (v5e), v3-8, cpu, or raw shape name",
        ),
    ] = None,
    internet: Annotated[
        Optional[bool],
        typer.Option(
            "--internet/--no-internet",
            help="Override internet access (defaults to kernel metadata)",
        ),
    ] = None,
    dataset: Annotated[
        Optional[List[str]],
        typer.Option(
            "-d",
            "--dataset",
            help="Additional dataset(s) 'owner/slug' to attach (preserves existing datasets)",
        ),
    ] = None,
    quick: Annotated[
        bool,
        typer.Option("--quick", help="Quick save without running all cells (QUICK_SAVE)"),
    ] = False,
    wait: Annotated[
        bool,
        typer.Option("-w", "--wait", help="Wait for batch run to complete and print logs"),
    ] = False,
    output_dir: Annotated[
        Optional[str],
        typer.Option(
            "-o",
            "--output",
            help="Download batch output files to this local directory once complete (implies --wait)",
        ),
    ] = None,
):
    """Trigger a batch 'Save & Run All' commit for a kernel slug (inheriting its datasets/models/settings)."""
    prev = state.store.load()
    _, _, authed_user = _load_kaggle_auth()

    if not slug:
        slug = (prev.kernel_slug if prev and prev.kernel_slug else None) or (
            f"{authed_user}/mykaggle-batch-test" if authed_user else "carlesoctav/mykaggle-batch-test"
        )
    elif "/" not in slug:
        user = authed_user or "carlesoctav"
        slug = f"{user}/{slug}"

    owner, kernel_slug = slug.split("/", 1)

    # Fetch existing kernel metadata & source blob
    meta = {}
    blob = {}
    try:
        kinfo = _kaggle_rpc(
            "kernels.KernelsApiService",
            "GetKernel",
            {"userName": owner, "kernelSlug": kernel_slug},
        )
        meta = kinfo.get("metadata") or {}
        blob = kinfo.get("blob") or {}
    except Exception:
        pass

    kernel_type = blob.get("kernelType") or meta.get("kernelType") or (
        "notebook" if (file and file.endswith(".ipynb")) else "script"
    )
    language = blob.get("language") or meta.get("language") or "python"

    if file:
        if not os.path.exists(file):
            typer.echo(f"[mykaggle] File not found: {file}", err=True)
            raise typer.Exit(1)
        with open(file, "r", encoding="utf-8") as f:
            source_text = f.read()
    elif cmd:
        source_text = (
            "import subprocess, sys\n"
            f"res = subprocess.run(['bash', '-lc', {cmd!r}])\n"
            "sys.exit(res.returncode)\n"
        )
    else:
        source_text = blob.get("source") or ""
        if not source_text:
            typer.echo(
                f"[mykaggle] No existing source found for '{slug}'; pass -f <file> or -c <cmd>.",
                err=True,
            )
            raise typer.Exit(1)

    source_text = _AdaptSourceToKernelType(source_text, kernel_type)

    if machine is not None:
        resolved_shape = _MACHINE_SHAPES.get(machine.lower().strip(), machine.strip())
    else:
        resolved_shape = meta.get("machineShape") or ""

    resolved_internet = bool(meta.get("enableInternet", False)) if internet is None else internet

    datasets_list = list(meta.get("datasetDataSources") or [])
    if dataset:
        for ds in dataset:
            if ds not in datasets_list:
                datasets_list.append(ds)

    enable_gpu = bool(meta.get("enableGpu", False))
    enable_tpu = bool(meta.get("enableTpu", False))
    if machine is not None:
        enable_gpu = resolved_shape.startswith("Nvidia")
        enable_tpu = resolved_shape.startswith("Tpu")

    save_payload = {
        "slug": slug,
        "newTitle": meta.get("title") or kernel_slug,
        "text": source_text,
        "language": language,
        "kernelType": kernel_type,
        "isPrivate": bool(meta.get("isPrivate", True)),
        "enableGpu": enable_gpu,
        "enableTpu": enable_tpu,
        "enableInternet": resolved_internet,
        "datasetDataSources": datasets_list,
        "kernelDataSources": list(meta.get("kernelDataSources") or []),
        "competitionDataSources": list(meta.get("competitionDataSources") or []),
        "modelDataSources": list(meta.get("modelDataSources") or []),
        "categoryIds": list(meta.get("categoryIds") or []),
        "kernelExecutionType": "QUICK_SAVE" if quick else "SAVE_AND_RUN_ALL",
    }
    if meta.get("id"):
        save_payload["id"] = meta["id"]
    if meta.get("dockerImage"):
        save_payload["dockerImage"] = meta["dockerImage"]
    if resolved_shape:
        save_payload["machineShape"] = resolved_shape

    shape_label = resolved_shape or "CPU"
    typer.echo(
        f"[mykaggle] Submitting batch commit for '{slug}' "
        f"(type={kernel_type}, machine={shape_label}, internet={resolved_internet})..."
    )

    try:
        resp = _kaggle_rpc("kernels.KernelsApiService", "SaveKernel", save_payload)
    except Exception as e:
        typer.echo(f"[mykaggle] Commit failed: {e}", err=True)
        raise typer.Exit(1)

    if resp.get("error"):
        typer.echo(f"[mykaggle] Commit error: {resp['error']}", err=True)
        raise typer.Exit(1)

    ver = resp.get("versionNumber")
    url = resp.get("url")
    typer.echo(f"[mykaggle] Committed '{slug}' version {ver}: {url}")

    if wait or output_dir:
        typer.echo(f"[mykaggle] Waiting for '{slug}' batch execution to finish...")
        last_status = None
        while True:
            time.sleep(5.0)
            st_resp = _kaggle_rpc(
                "kernels.KernelsApiService",
                "GetKernelSessionStatus",
                {"userName": owner, "kernelSlug": kernel_slug},
            )
            status = st_resp.get("status")
            fail_msg = st_resp.get("failureMessage")
            if status != last_status:
                typer.echo(f"[mykaggle] Status: {status}")
                last_status = status
            if status in ("COMPLETE", "ERROR", "CANCEL_ACKNOWLEDGED", "CANCEL_REQUESTED"):
                if fail_msg:
                    typer.echo(f"[mykaggle] Failure message: {fail_msg}", err=True)
                break

        out_resp = _kaggle_rpc(
            "kernels.KernelsApiService",
            "ListKernelSessionOutput",
            {"userName": owner, "kernelSlug": kernel_slug},
        )
        log_text = out_resp.get("log")
        if log_text:
            try:
                log_entries = json.loads(log_text)
                for entry in log_entries:
                    sys.stdout.write(entry.get("data", ""))
                sys.stdout.flush()
            except Exception:
                typer.echo(log_text)

        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            files = out_resp.get("files") or []
            for fitem in files:
                fname = fitem.get("fileName")
                furl = fitem.get("url")
                if not fname or not furl:
                    continue
                dest = os.path.join(output_dir, fname)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                r = requests.get(furl, timeout=60)
                r.raise_for_status()
                with open(dest, "wb") as wf:
                    wf.write(r.content)
                typer.echo(f"[mykaggle] Downloaded: {dest} ({len(r.content)} bytes)")

        if last_status != "COMPLETE":
            raise typer.Exit(1)


def ls_command(
    search: Annotated[
        Optional[str],
        typer.Option("-s", "--search", help="Search query filter for kernels"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("-n", "--limit", help="Maximum number of kernels to list"),
    ] = 20,
    table: Annotated[
        bool,
        typer.Option(
            "--table",
            help="Render output as a human-readable table instead of JSON",
        ),
    ] = False,
):
    """List Kaggle kernels and show currently active connected session."""
    _, _, authed_user = _load_kaggle_auth()
    user = authed_user or "carlesoctav"

    payload = {"user": user, "group": "PROFILE", "pageSize": limit}
    if search:
        payload["search"] = search

    try:
        res = _kaggle_rpc("kernels.KernelsApiService", "ListKernels", payload)
    except Exception as e:
        typer.echo(f"[mykaggle] Failed to list kernels: {e}", err=True)
        raise typer.Exit(1)

    kernels_list = res.get("kernels") or []

    prev = state.store.load()
    active_slug = prev.kernel_slug if prev else None
    active_ks_id = prev.kernel_session_id if prev else None
    active_kid = prev.kernel_id if prev else None
    connected_at = prev.connected_at if prev else None

    if not table:
        connected_session = None
        if prev and (active_slug or active_ks_id or active_kid):
            connected_session = {
                "slug": active_slug,
                "kernel_session_id": active_ks_id,
                "kernel_id": active_kid,
                "connected_at": connected_at,
            }

        kernels_data = []
        for k in kernels_list:
            ref = k.get("ref", "")
            title = k.get("title", "")
            last_run = k.get("lastRunTime", "")
            if last_run and "T" in last_run:
                last_run = last_run.replace("T", " ")[:19]
            is_active = bool(active_slug and (ref == active_slug or ref.endswith(f"/{active_slug}")))
            kernels_data.append({
                "ref": ref,
                "title": title,
                "last_run_time": last_run,
                "is_active": is_active,
            })

        out = {
            "connected_session": connected_session,
            "kernels": kernels_data,
        }
        typer.echo(json.dumps(out, indent=2))
        return

    if prev and (active_slug or active_ks_id):
        _console.print(
            f"[bold green]Connected Session:[/bold green] slug=[bold]{active_slug or '-'}[/bold] "
            f"session_id=[cyan]{active_ks_id or '-'}[/cyan] connected_at=[dim]{connected_at or '-'}[/dim]"
        )

    rich_table = Table(title=f"Kaggle Kernels ({user})", show_header=True)
    rich_table.add_column("Ref", style="cyan")
    rich_table.add_column("Title")
    rich_table.add_column("Last Run", style="dim")
    rich_table.add_column("Status", style="bold")

    for k in kernels_list:
        ref = k.get("ref", "")
        title = k.get("title", "")
        last_run = k.get("lastRunTime", "")
        if last_run and "T" in last_run:
            last_run = last_run.replace("T", " ")[:19]
        is_active = bool(active_slug and (ref == active_slug or ref.endswith(f"/{active_slug}")))
        status_label = "[green]ACTIVE[/green]" if is_active else ""
        ref_label = f"[bold green]{ref}[/bold green]" if is_active else ref
        rich_table.add_row(ref_label, title, last_run, status_label)

    _console.print(rich_table)


session_app = typer.Typer(
    help="Manage Kaggle kernel and VM sessions.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

session_app.command(name="connect")(connect_command)
session_app.command(name="create")(create_command)
session_app.command(name="start")(start_command)
session_app.command(name="delete")(delete_command)
session_app.command(name="stop")(stop_command)
session_app.command(name="ls")(ls_command)
