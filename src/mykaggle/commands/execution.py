import datetime
import os
import sys
import time
import uuid
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from typing_extensions import Annotated

from mykaggle.common import make_runtime, resolve_session, state
from mykaggle.contents import ContentsClient
from mykaggle.repl import KaggleREPL
from mykaggle.runtime import KaggleRuntime
from mykaggle.state import KernelInfo
from mykaggle.utils import print_output

_console = Console()


def _format_cell_code(command: str, log_file: str) -> str:
    """Format shell command to run via bash with line-buffered logging to /kaggle/working."""
    import base64
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


def _get_or_create_named_kernel(
    name: str, command_desc: str, log_file: str
) -> tuple[KaggleRuntime, KernelInfo]:
    """Reuse existing named kernel if still alive, or spawn a new one."""
    s = resolve_session()
    k_info = s.kernels.get(name)

    if k_info:
        # Check if the kernel is actually alive on the server
        test_rt = KaggleRuntime(s.url, kernel_id=k_info.kernel_id)
        alive_kernels = {k["id"] for k in test_rt.list_kernels()}
        if k_info.kernel_id in alive_kernels:
            rt = KaggleRuntime(
                s.url,
                kernel_id=k_info.kernel_id,
                session_id=k_info.session_id,
            )
            k_info.command = command_desc
            s.kernels[name] = k_info
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
    state.store.save(s)
    return rt, k_info


def run_command(
    name_or_cmd: Annotated[
        str,
        typer.Argument(
            help="Name of the job (if command follows), or the command itself."
        ),
    ],
    command: Annotated[
        Optional[str],
        typer.Argument(help="Optional command to execute if name was specified first."),
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
    """Run a shell command on a dedicated kernel (reuses kernel if name matches)."""
    if command is not None:
        name = name_or_cmd
        actual_cmd = command
    else:
        actual_cmd = name_or_cmd
        name = f"job-{uuid.uuid4().hex[:6]}"

    log_file = f".mykaggle/logs/{name}.log"
    rt, k_info = _get_or_create_named_kernel(name, actual_cmd, log_file)
    typer.echo(f"[mykaggle] Running [{name}] on kernel {rt.kernel_id}...")

    py_code = _format_cell_code(actual_cmd, log_file)

    if detach:
        rt.execute_detached(py_code)
        rt.stop(shutdown_kernel=False)
        typer.echo(f"[mykaggle] Job [{name}] detached. Follow logs with: mykaggle logs {name}")
        return

    try:
        rt.execute_code(
            py_code,
            output_hook=lambda o: print_output(o, _console, output_image),
            timeout=timeout,
        )
    except KeyboardInterrupt:
        typer.echo(f"\n[mykaggle] Detached from [{name}]. Job is still running.")
        typer.echo(f"[mykaggle] View logs with: mykaggle logs {name}")
    finally:
        rt.stop(shutdown_kernel=False)


def run_file_command(
    name_or_file: Annotated[
        str,
        typer.Argument(
            help="Name of the job (if file follows), or path to Python file."
        ),
    ],
    file_path: Annotated[
        Optional[str],
        typer.Argument(help="Path to Python script if name was specified first."),
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
    """Run a Python file on a dedicated kernel without uploading it."""
    if file_path is not None:
        name = name_or_file
        path = file_path
    else:
        path = name_or_file
        name = os.path.basename(path)

    if not os.path.isfile(path):
        typer.echo(f"[mykaggle] File not found: {path}", err=True)
        raise typer.Exit(2)

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        file_content = f.read()

    log_file = f".mykaggle/logs/{name}.log"
    rt, k_info = _get_or_create_named_kernel(name, f"python {path}", log_file)
    typer.echo(f"[mykaggle] Running file [{name}] on kernel {rt.kernel_id}...")

    # We base64 encode the script so multi-line code, quotes, and backslashes
    # never get mangled over the shell/IPC transport.
    import base64
    b64_src = base64.b64encode(file_content.encode("utf-8")).decode("ascii")
    runner_code = f"""
import base64, subprocess, sys, os
os.makedirs(os.path.dirname('/kaggle/working/{log_file}'), exist_ok=True)
_src = base64.b64decode({b64_src!r})
_log_f = open('/kaggle/working/{log_file}', 'a')
_p = subprocess.Popen(
    [sys.executable, '-u', '-'],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
)
_p.stdin.write(_src)
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

    if detach:
        rt.execute_detached(runner_code)
        rt.stop(shutdown_kernel=False)
        typer.echo(f"[mykaggle] File [{name}] detached. Follow logs with: mykaggle logs {name}")
        return

    try:
        rt.execute_code(
            runner_code,
            output_hook=lambda o: print_output(o, _console, output_image),
            timeout=timeout,
        )
    except KeyboardInterrupt:
        typer.echo(f"\n[mykaggle] Detached from [{name}]. Job is still running.")
        typer.echo(f"[mykaggle] View logs with: mykaggle logs {name}")
    finally:
        rt.stop(shutdown_kernel=False)


def logs_command(
    name: Annotated[
        Optional[str],
        typer.Argument(help="Name of the kernel/job to view logs for. Omit to list all."),
    ] = None,
    follow: Annotated[
        bool, typer.Option("-f", "--follow", help="Continuously poll / stream logs")
    ] = False,
    lines: Annotated[
        int, typer.Option("-n", "--lines", help="Number of lines to show from log")
    ] = 40,
):
    """View logs of a running/completed job, or list all active jobs."""
    s = resolve_session()
    rt = KaggleRuntime(s.url)
    remote_kernels = {k["id"]: k for k in rt.list_kernels()}

    if not name:
        # List all known kernels/jobs
        table = Table(title="Kaggle Kernels / Jobs", show_header=True)
        table.add_column("Name", style="cyan")
        table.add_column("Kernel ID", style="dim")
        table.add_column("State", style="bold")
        table.add_column("Started At")
        table.add_column("Command")

        # Show notebook / primary kernel
        prim = remote_kernels.get(s.kernel_id or "")
        p_state = prim.get("execution_state", "unknown") if prim else "offline"
        table.add_row("(primary/notebook)", s.kernel_id or "-", p_state, s.connected_at or "-", "-")

        # Show named kernels
        for k_name, k_info in s.kernels.items():
            k_data = remote_kernels.get(k_info.kernel_id)
            status = k_data.get("execution_state", "offline") if k_data else "terminated"
            table.add_row(
                k_name,
                k_info.kernel_id[:8] + "...",
                status,
                k_info.started_at,
                (k_info.command or "-")[:35],
            )

        _console.print(table)
        return

    # Specific job logs
    k_info = s.kernels.get(name)
    log_path = k_info.log_path if k_info else f".mykaggle/logs/{name}.log"
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
                typer.echo(f"[mykaggle] No log file found for [{name}] at {log_path}", err=True)
                break
            time.sleep(1.0)


def stop_command(
    name: Annotated[str, typer.Argument(help="Name of the kernel/job to stop and delete.")],
):
    """Stop and delete a kernel (alias: rm)."""
    s = resolve_session()
    k_info = s.kernels.get(name)
    if not k_info:
        typer.echo(f"[mykaggle] No known kernel with name [{name}].", err=True)
        raise typer.Exit(1)

    rt = KaggleRuntime(s.url, kernel_id=k_info.kernel_id)
    rt.delete_kernel()
    del s.kernels[name]
    state.store.save(s)
    typer.echo(f"[mykaggle] Stopped and deleted kernel [{name}] ({k_info.kernel_id}).")


def interrupt_command(
    name: Annotated[str, typer.Argument(help="Name of the kernel to interrupt (SIGINT).")],
):
    """Send SIGINT interrupt to a kernel."""
    s = resolve_session()
    k_info = s.kernels.get(name)
    kid = k_info.kernel_id if k_info else name
    rt = KaggleRuntime(s.url, kernel_id=kid)
    rt.interrupt()
    typer.echo(f"[mykaggle] Sent interrupt to kernel [{name}].")


def restart_command(
    name: Annotated[str, typer.Argument(help="Name of the kernel to restart.")],
):
    """Restart a kernel."""
    s = resolve_session()
    k_info = s.kernels.get(name)
    kid = k_info.kernel_id if k_info else name
    rt = KaggleRuntime(s.url, kernel_id=kid)
    rt.restart()
    typer.echo(f"[mykaggle] Restarted kernel [{name}].")


def exec_command(
    code: Annotated[Optional[str], typer.Argument(help="Code to execute")] = None,
    output_image: Annotated[
        Optional[str], typer.Option("--output-image", help="Path to save plot")
    ] = None,
    timeout: Annotated[
        Optional[float],
        typer.Option("--timeout", help="Timeout in seconds for code execution"),
    ] = None,
):
    """Execute raw code on the primary notebook kernel."""
    s = resolve_session()
    runtime = make_runtime(s)

    if code is None:
        if sys.stdin.isatty():
            typer.echo("[mykaggle] Provide CODE or pipe code on stdin.", err=True)
            raise typer.Exit(2)
        code = sys.stdin.read()

    try:
        runtime.execute_code(
            code,
            output_hook=lambda o: print_output(o, _console, output_image),
            timeout=timeout,
        )
        print()
    finally:
        runtime.stop(shutdown_kernel=False)


def repl(
    output_image: Annotated[
        Optional[str], typer.Option("--output-image", help="Path to save plot")
    ] = None,
):
    """Start an interactive REPL on the primary notebook kernel."""
    s = resolve_session()
    runtime = make_runtime(s)
    KaggleREPL(runtime, output_image=output_image).run()


def register(app: typer.Typer):
    app.command(name="run")(run_command)
    app.command(name="run-file")(run_file_command)
    app.command(name="logs")(logs_command)
    app.command(name="stop")(stop_command)
    app.command(name="rm")(stop_command)
    app.command(name="int")(interrupt_command)
    app.command(name="restart")(restart_command)
    app.command(name="exec")(exec_command)
    app.command()(repl)
