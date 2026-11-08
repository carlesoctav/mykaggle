from pathlib import Path
from typing import Optional

import click
import typer
from typer.core import TyperGroup
from typing_extensions import Annotated

from mykaggle.commands.execution import process_app
from mykaggle.commands.session import connect_command, session_app
from mykaggle.commands.uv_cmd import uv_app
from mykaggle.common import setup_logging, state


class MykaggleGroup(TyperGroup):
    """Alphabetical listing + `mykaggle <url>` shorthand for connect.

    A group-level argument would swallow the subcommand name during
    parsing, so instead an unknown first token is rewritten to
    `connect <token>...` here.
    """

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted(super().list_commands(ctx))

    def resolve_command(self, ctx: click.Context, args: list[str]):
        if args and self.get_command(ctx, args[0]) is None:
            args = ["connect", *args]
        return super().resolve_command(ctx, args)


app = typer.Typer(
    help="Kaggle CLI (colab-cli style, one session at a time).\n\nConnect: mykaggle session connect <compatible-url>",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    cls=MykaggleGroup,
)


@app.callback(invoke_without_command=True)
def callback(
    ctx: typer.Context,
    config: Annotated[
        Optional[str],
        typer.Option(
            "--config",
            help="Path to session state file (~/.config/mykaggle/session.json)",
        ),
    ] = None,
    logtostderr: Annotated[
        bool, typer.Option("--logtostderr", help="Log all output to stderr")
    ] = False,
):
    """
    mykaggle global configuration.
    """
    state.config_path = config
    state.logtostderr = logtostderr
    setup_logging(logtostderr)

    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


@app.command(name="help")
def help_command(
    ctx: typer.Context,
    command: Annotated[
        Optional[str], typer.Argument(help="Command to show help for")
    ] = None,
):
    """
    Show help for a command.
    """
    if not command:
        typer.echo(ctx.parent.get_help())
        return

    group = ctx.parent.command
    cmd = group.get_command(ctx, command)
    if cmd is None:
        typer.echo(f"No such command '{command}'.", err=True)
        raise typer.Exit(code=2)

    with click.Context(cmd, info_name=command, parent=ctx.parent) as cmd_ctx:
        typer.echo(cmd.get_help(cmd_ctx))


@app.command(name="skill")
def skill_command():
    """
    Print the mykaggle agent skill instructions (SKILL.md).
    """
    candidates = [
        Path(__file__).resolve().parent / "SKILL.md",
        Path(__file__).resolve().parents[2] / "skills" / "mykaggle" / "SKILL.md",
        Path.home() / ".agents" / "skills" / "mykaggle" / "SKILL.md",
    ]
    for p in candidates:
        if p.is_file():
            typer.echo(p.read_text(encoding="utf-8"), nl=False)
            return
    typer.echo("SKILL.md not found.", err=True)
    raise typer.Exit(code=1)


# Register subapps
app.add_typer(session_app, name="session")
app.add_typer(process_app, name="process")
app.add_typer(uv_app, name="uv")

# Hidden connect command for `mykaggle <url>` callback
app.command(name="connect", hidden=True)(connect_command)


def main():
    app()


if __name__ == "__main__":
    main()
