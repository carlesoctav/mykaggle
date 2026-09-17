import os
import shlex
from typing import List, Optional

import typer
from rich.console import Console
from typing_extensions import Annotated

from mykaggle.common import make_runtime, resolve_session
from mykaggle.contents import ContentsClient, KAGGLE_WORKING_DIR
from mykaggle.utils import print_output

_console = Console()


def build_install_args(packages, requirements_remote=None):
    """Flatten install targets into pip-style argv (requirements first).

    >>> build_install_args(["a", "b"], "/kaggle/working/r.txt")
    ['-r', '/kaggle/working/r.txt', 'a', 'b']
    >>> build_install_args(["a"])
    ['a']
    >>> build_install_args([], "/kaggle/working/r.txt")
    ['-r', '/kaggle/working/r.txt']
    """
    commands = []
    if requirements_remote:
        commands.extend(["-r", requirements_remote])
    if packages:
        commands.extend(packages)
    return commands


def build_install_code(commands):
    """Build a `!uv pip install` shell line (uv is assumed present).

    --no-progress: uv's spinners are \\r-only, which Rust block-buffers when
    piped, so without this flag long installs show nothing until the end.

    >>> build_install_code(['-r', '/kaggle/working/r.txt', 'a'])
    '!uv pip install --system --no-progress -r /kaggle/working/r.txt a'
    >>> build_install_code(['a[extra]'])
    "!uv pip install --system --no-progress 'a[extra]'"
    """
    return "!uv pip install --system --no-progress " + " ".join(
        shlex.quote(c) for c in commands
    )


def install_command(
    packages: Annotated[
        Optional[List[str]], typer.Argument(help="Packages to install")
    ] = None,
    requirements: Annotated[
        Optional[str],
        typer.Option("-r", "--requirement", help="Requirements file to install"),
    ] = None,
    timeout: Annotated[
        Optional[float],
        typer.Option("--timeout", help="Timeout in seconds for installation"),
    ] = None,
):
    """Install python packages on the connected kernel (preferring uv)."""
    if not packages and not requirements:
        typer.echo(
            "[mykaggle] Specify packages or -r requirements file.", err=True
        )
        raise typer.Exit(2)

    s = resolve_session()
    runtime = make_runtime(s)

    remote_abs = None
    if requirements:
        if not os.path.isfile(requirements):
            typer.echo(f"[mykaggle] File not found: {requirements}", err=True)
            raise typer.Exit(2)
        remote_name = os.path.basename(requirements)
        ContentsClient(s.url).upload(requirements, remote_name)
        remote_abs = f"{KAGGLE_WORKING_DIR}/{remote_name}"
        typer.echo(f"[mykaggle] Uploaded {requirements} -> {remote_abs}.")

    code = build_install_code(build_install_args(packages, remote_abs))
    _console.print(f"[dim]{code}[/dim]")
    try:
        runtime.execute_code(
            code,
            output_hook=lambda o: print_output(o, _console),
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


def register(app: typer.Typer):
    app.command(name="install")(install_command)
