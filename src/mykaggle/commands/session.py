import datetime
import uuid
from urllib.parse import urlparse

import requests
import typer
from typing_extensions import Annotated

from mykaggle.common import state
from mykaggle.state import SessionState
from mykaggle.urls import redact, rest_url, split_server_url


def connect_to(url: str) -> SessionState:
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

    session = SessionState(
        url=url,
        kernel_id=kernel_id,
        session_id=uuid.uuid4().hex,
        connected_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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


def register(app: typer.Typer):
    app.command(name="connect")(connect_command)
