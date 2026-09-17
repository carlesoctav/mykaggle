import os
import shlex
import sys
from typing import Optional

import typer
from rich.console import Console
from typing_extensions import Annotated

from mykaggle.common import make_runtime, resolve_session
from mykaggle.contents import ContentsClient, KAGGLE_WORKING_DIR
from mykaggle.repl import KaggleREPL
from mykaggle.utils import print_output

_console = Console()


def exec_command(
    code: Annotated[
        Optional[str], typer.Argument(help="Code to execute")
    ] = None,
    file: Annotated[
        Optional[str], typer.Option("-f", "--file", help="File to upload and run")
    ] = None,
    output_image: Annotated[
        Optional[str], typer.Option("--output-image", help="Path to save plot")
    ] = None,
    timeout: Annotated[
        Optional[float],
        typer.Option("--timeout", help="Timeout in seconds for code execution"),
    ] = None,
):
    """Execute code on the connected kernel."""
    s = resolve_session()
    runtime = make_runtime(s)

    if file:
        if not os.path.isfile(file):
            typer.echo(f"[mykaggle] File not found: {file}", err=True)
            raise typer.Exit(2)
        remote_name = os.path.basename(file)
        ContentsClient(s.url).upload(file, remote_name)
        remote_abs = f"{KAGGLE_WORKING_DIR}/{remote_name}"
        typer.echo(f"[mykaggle] Uploaded {file} -> {remote_abs}.")
        if remote_name.endswith((".pyz", ".zip")):
            # Archives are binary; %run reads source as text, so shell out.
            code = f"!python {shlex.quote(remote_abs)}"
        else:
            code = f"%run {shlex.quote(remote_abs)}"
    elif code is None:
        if sys.stdin.isatty():
            typer.echo(
                "[mykaggle] Provide CODE, -f FILE, or pipe code on stdin.", err=True
            )
            raise typer.Exit(2)
        code = sys.stdin.read()

    try:
        runtime.execute_code(
            code,
            output_hook=lambda o: print_output(o, _console, output_image),
            timeout=timeout,
        )
        print()
    except TimeoutError as e:
        typer.echo(f"[mykaggle] {e}", err=True)
        raise typer.Exit(1)
    except RuntimeError as e:
        typer.echo(f"[mykaggle] {e}", err=True)
        raise typer.Exit(1)
    finally:
        runtime.stop()


def repl(
    output_image: Annotated[
        Optional[str], typer.Option("--output-image", help="Path to save plot")
    ] = None,
):
    """Start an interactive REPL on the connected kernel."""
    s = resolve_session()
    runtime = make_runtime(s)
    KaggleREPL(runtime, output_image=output_image).run()


def register(app: typer.Typer):
    app.command(name="exec")(exec_command)
    app.command()(repl)
