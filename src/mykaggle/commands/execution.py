import base64
import datetime
import json
import os
import sys
import time
import uuid
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from typing_extensions import Annotated

from mykaggle.commands.session import attach_command, commit_command
from mykaggle.common import resolve_session, state
from mykaggle.contents import ContentsClient
from mykaggle.runtime import KaggleRuntime
from mykaggle.state import KernelInfo
from mykaggle.utils import print_output

_console = Console()


def _format_cell_code(command: str, log_file: str) -> str:
    """Format shell command to run via bash with line-buffered logging to /kaggle/working."""
    b64_cmd = base64.b64encode(command.encode("utf-8")).decode("ascii")
    return f"""
import base64, subprocess, sys, os
os.makedirs(os.path.dirname('/kaggle/working/{log_file}'), exist_ok=True)
_cmd = base64.b64decode({b64_cmd!r}).decode('utf-8')
_log_f = open('/kaggle/working/{log_file}', 'a')
_p = subprocess.Popen(
    _cmd,
    shell=True,
    executable='/bin/bash',
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
)
for _line in iter(_p.stdout.readline, b''):
    _text = _line.decode('utf-8', errors='replace')
    sys.stdout.write(_text)
    sys.stdout.flush()
    _log_f.write(_text)
    _log_f.flush()
_p.wait()
_log_f.close()
"""


def _format_file_runner_code(
    file_content: str, script_name: str, extra_args: list[str], log_file: str
) -> str:
    """Format Python script runner with sys.argv injection and line-buffered logging."""
    b64_src = base64.b64encode(file_content.encode("utf-8")).decode("ascii")
    preamble = f"import sys\nsys.argv = [{script_name!r}] + {extra_args!r}\n"
    b64_preamble = base64.b64encode(preamble.encode("utf-8")).decode("ascii")
    return f"""
import base64, subprocess, sys, os
os.makedirs(os.path.dirname('/kaggle/working/{log_file}'), exist_ok=True)
_preamble = base64.b64decode({b64_preamble!r})
_src = base64.b64decode({b64_src!r})
_log_f = open('/kaggle/working/{log_file}', 'a')
_cmd = [sys.executable, '-u', '-'] + {extra_args!r}
_p = subprocess.Popen(
    _cmd,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
)
_p.stdin.write(_preamble + _src)
_p.stdin.close()
for _line in iter(_p.stdout.readline, b''):
    _text = _line.decode('utf-8', errors='replace')
    sys.stdout.write(_text)
    sys.stdout.flush()
    _log_f.write(_text)
    _log_f.flush()
_p.wait()
_log_f.close()
"""


def _get_or_create_named_kernel(
    name: str, command_desc: str, log_file: str
) -> tuple[KaggleRuntime, KernelInfo]:
    """Reuse existing named kernel if still alive, or spawn a new one.

    If name is 'main', reuse the main notebook kernel (s.kernel_id or
    s.kernels['main']). If name is a custom name, reuse s.kernels[name] or
    spawn a dedicated kernel.
    """
    s = resolve_session()

    target_kid = None
    target_sid = None
    if name == "main":
        k_info = s.kernels.get("main")
        if k_info:
            target_kid = k_info.kernel_id
            target_sid = k_info.session_id
        elif s.kernel_id:
            target_kid = s.kernel_id
            target_sid = s.session_id
    else:
        k_info = s.kernels.get(name)
        if k_info:
            target_kid = k_info.kernel_id
            target_sid = k_info.session_id

    if target_kid:
        test_rt = KaggleRuntime(s.url, kernel_id=target_kid)
        try:
            alive_kernels = {k["id"] for k in test_rt.list_kernels()}
        except Exception:
            alive_kernels = set()
        if target_kid in alive_kernels:
            rt = KaggleRuntime(
                s.url,
                kernel_id=target_kid,
                session_id=target_sid,
            )
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            prev_k = s.kernels.get(name)
            k_info = KernelInfo(
                name=name,
                kernel_id=target_kid,
                session_id=target_sid or uuid.uuid4().hex,
                started_at=prev_k.started_at if (prev_k and prev_k.started_at) else now_str,
                command=command_desc,
                log_path=log_file,
            )
            s.kernels[name] = k_info
            if name == "main":
                s.kernel_id = target_kid
            state.store.save(s)
            return rt, k_info

    # Spawn fresh kernel
    rt = KaggleRuntime(s.url)
    kid = rt.spawn_new_kernel()
    sid = uuid.uuid4().hex
    rt.session_id = sid

    k_info = KernelInfo(
        name=name,
        kernel_id=kid,
        session_id=sid,
        started_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        command=command_desc,
        log_path=log_file,
    )
    s.kernels[name] = k_info
    if name == "main":
        s.kernel_id = kid
    state.store.save(s)
    return rt, k_info


def run_command(
    ctx: typer.Context,
    command: Annotated[
        Optional[str],
        typer.Argument(help="Shell command to execute"),
    ] = None,
    name: Annotated[
        Optional[str],
        typer.Option("-n", "--name", help="Job/kernel name (defaults to 'main')"),
    ] = None,
    file: Annotated[
        Optional[str],
        typer.Option("-f", "--file", help="Local Python file to execute on remote kernel"),
    ] = None,
    detach: Annotated[
        bool,
        typer.Option("-d", "--detach", help="Run in background without streaming output"),
    ] = False,
    output_image: Annotated[
        Optional[str], typer.Option("--output-image", help="Path to save plot")
    ] = None,
    timeout: Annotated[
        Optional[float],
        typer.Option("--timeout", help="Timeout in seconds for code execution"),
    ] = None,
):
    """Run a shell command or Python file on a remote kernel (reuses kernel if name matches)."""
    job_name = name or "main"
    log_file = f".mykaggle/logs/{job_name}.log"

    if file:
        if not os.path.isfile(file):
            typer.echo(f"[mykaggle] File not found: {file}", err=True)
            raise typer.Exit(2)

        extra_args = ([command] if command else []) + list(ctx.args)
        with open(file, "r", encoding="utf-8", errors="replace") as f:
            file_content = f.read()

        arg_desc = f" {' '.join(extra_args)}" if extra_args else ""
        command_desc = f"python {os.path.basename(file)}{arg_desc}"
        rt, k_info = _get_or_create_named_kernel(job_name, command_desc, log_file)
        typer.echo(f"[mykaggle] Running file [{job_name}] on kernel {rt.kernel_id}...")

        py_code = _format_file_runner_code(
            file_content, os.path.basename(file), extra_args, log_file
        )
    else:
        if not command and not ctx.args:
            typer.echo("[mykaggle] Specify COMMAND or -f/--file FILE.", err=True)
            raise typer.Exit(2)

        actual_cmd = " ".join(([command] if command else []) + list(ctx.args))
        command_desc = actual_cmd
        rt, k_info = _get_or_create_named_kernel(job_name, command_desc, log_file)
        typer.echo(f"[mykaggle] Running [{job_name}] on kernel {rt.kernel_id}...")

        py_code = _format_cell_code(actual_cmd, log_file)

    if detach:
        rt.execute_detached(py_code)
        rt.stop(shutdown_kernel=False)
        typer.echo(f"[mykaggle] Job [{job_name}] detached. Follow logs with: mykaggle process logs {job_name}")
        return

    try:
        rt.execute_code(
            py_code,
            output_hook=lambda o: print_output(o, _console, output_image),
            timeout=timeout,
        )
    except KeyboardInterrupt:
        typer.echo(f"\n[mykaggle] Detached from [{job_name}]. Job is still running.")
        typer.echo(f"[mykaggle] View logs with: mykaggle process logs {job_name}")
    finally:
        rt.stop(shutdown_kernel=False)


def interrupt_command(
    name_arg: Annotated[
        Optional[str],
        typer.Argument(help="Name of the kernel to interrupt (defaults to 'main')"),
    ] = None,
    name: Annotated[
        Optional[str],
        typer.Option("-n", "--name", help="Name of the kernel to interrupt"),
    ] = None,
):
    """Send SIGINT interrupt to a kernel process."""
    target = name or name_arg or "main"
    s = resolve_session()
    k_info = s.kernels.get(target)
    if k_info:
        kid = k_info.kernel_id
    elif target == "main" and s.kernel_id:
        kid = s.kernel_id
    else:
        kid = target

    rt = KaggleRuntime(s.url, kernel_id=kid)
    rt.interrupt()
    typer.echo(f"[mykaggle] Sent interrupt to kernel [{target}].")


def logs_command(
    name_arg: Annotated[
        Optional[str],
        typer.Argument(help="Name of the kernel/job to view logs for. Omit to list all."),
    ] = None,
    name: Annotated[
        Optional[str],
        typer.Option("-n", "--name", help="Name of the kernel/job to view logs for"),
    ] = None,
    follow: Annotated[
        bool, typer.Option("-f", "--follow", help="Continuously poll / stream logs")
    ] = False,
    lines: Annotated[
        int, typer.Option("--lines", help="Number of lines to show from log")
    ] = 40,
    table: Annotated[
        bool,
        typer.Option(
            "--table",
            help="Render output as a human-readable table instead of JSON",
        ),
    ] = False,
):
    """View logs of a running/completed job, or list all recorded jobs/kernels."""
    target = name or name_arg
    s = resolve_session()
    rt = KaggleRuntime(s.url)
    try:
        remote_kernels = {k["id"]: k for k in rt.list_kernels()}
    except Exception:
        remote_kernels = {}

    if not target:
        if not table:
            jobs_list = []
            # Show main / primary kernel
            main_kinfo = s.kernels.get("main")
            main_kid = main_kinfo.kernel_id if main_kinfo else s.kernel_id
            if main_kid:
                m_data = remote_kernels.get(main_kid)
                m_state = m_data.get("execution_state", "offline") if m_data else "terminated"
                jobs_list.append({
                    "name": "main",
                    "kernel_id": main_kid,
                    "status": m_state,
                    "command": main_kinfo.command if main_kinfo else None,
                    "started_at": (main_kinfo.started_at if main_kinfo else s.connected_at),
                    "log_path": main_kinfo.log_path if main_kinfo else ".mykaggle/logs/main.log",
                })

            # Show other named kernels
            for k_name, k_info in s.kernels.items():
                if k_name == "main":
                    continue
                k_data = remote_kernels.get(k_info.kernel_id)
                status = k_data.get("execution_state", "offline") if k_data else "terminated"
                jobs_list.append({
                    "name": k_name,
                    "kernel_id": k_info.kernel_id,
                    "status": status,
                    "command": k_info.command,
                    "started_at": k_info.started_at,
                    "log_path": k_info.log_path,
                })

            typer.echo(json.dumps(jobs_list, indent=2))
            return

        rich_table = Table(title="Kaggle Kernels / Jobs", show_header=True)
        rich_table.add_column("Name", style="cyan")
        rich_table.add_column("Kernel ID", style="dim")
        rich_table.add_column("State", style="bold")
        rich_table.add_column("Started At")
        rich_table.add_column("Command")

        # Show main / primary kernel
        main_kinfo = s.kernels.get("main")
        main_kid = main_kinfo.kernel_id if main_kinfo else s.kernel_id
        if main_kid:
            m_data = remote_kernels.get(main_kid)
            m_state = m_data.get("execution_state", "offline") if m_data else "terminated"
            rich_table.add_row(
                "main",
                main_kid[:8] + "...",
                m_state,
                (main_kinfo.started_at if main_kinfo else s.connected_at) or "-",
                ((main_kinfo.command if main_kinfo else None) or "-")[:35],
            )

        # Show other named kernels
        for k_name, k_info in s.kernels.items():
            if k_name == "main":
                continue
            k_data = remote_kernels.get(k_info.kernel_id)
            status = k_data.get("execution_state", "offline") if k_data else "terminated"
            rich_table.add_row(
                k_name,
                k_info.kernel_id[:8] + "...",
                status,
                k_info.started_at,
                (k_info.command or "-")[:35],
            )

        _console.print(rich_table)
        return

    # Specific job logs
    k_info = s.kernels.get(target)
    log_path = k_info.log_path if k_info else f".mykaggle/logs/{target}.log"
    contents = ContentsClient(s.url)

    last_pos = 0
    while True:
        try:
            data = contents._request("GET", log_path)
            content = data.get("content", "")
            if follow:
                if len(content) > last_pos:
                    sys.stdout.write(content[last_pos:])
                    sys.stdout.flush()
                    last_pos = len(content)
                time.sleep(1.0)
            else:
                log_lines = content.splitlines()
                for l in log_lines[-lines:]:
                    print(l)
                break
        except Exception:
            if not follow:
                typer.echo(f"[mykaggle] No log file found for [{target}] at {log_path}", err=True)
                break
            time.sleep(1.0)


def rm_command(
    name_arg: Annotated[
        Optional[str],
        typer.Argument(help="Name of the kernel/job to stop and delete"),
    ] = None,
    name: Annotated[
        Optional[str],
        typer.Option("-n", "--name", help="Name of the kernel/job to stop and delete"),
    ] = None,
):
    """Stop and delete a Jupyter kernel process."""
    target = name or name_arg
    if not target:
        typer.echo("[mykaggle] Specify kernel name to remove (e.g. 'main' or a named job).", err=True)
        raise typer.Exit(2)

    s = resolve_session()
    k_info = s.kernels.get(target)
    kid = None
    if k_info:
        kid = k_info.kernel_id
    elif target == "main" and s.kernel_id:
        kid = s.kernel_id
    else:
        try:
            remote_kids = {k["id"] for k in KaggleRuntime(s.url).list_kernels()}
            if target in remote_kids:
                kid = target
        except Exception:
            pass

    if not kid:
        typer.echo(f"[mykaggle] No known kernel with name [{target}].", err=True)
        raise typer.Exit(1)

    rt = KaggleRuntime(s.url, kernel_id=kid)
    rt.delete_kernel()
    if target in s.kernels:
        del s.kernels[target]
    if target == "main" or kid == s.kernel_id:
        s.kernel_id = None
    state.store.save(s)
    typer.echo(f"[mykaggle] Stopped and deleted kernel [{target}] ({kid}).")


process_app = typer.Typer(
    help="Manage processes and code execution on connected Kaggle sessions.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

process_app.command(name="attach")(attach_command)
process_app.command(
    name="run",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)(run_command)
process_app.command(name="int")(interrupt_command)
process_app.command(name="logs")(logs_command)
process_app.command(name="rm")(rm_command)
process_app.command(name="commit")(commit_command)
