from typing import Optional

import click
import typer
from typer.core import TyperGroup
from typing_extensions import Annotated

from mykaggle.commands import execution, packages, session
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
    help="Kaggle CLI (colab-cli style, one session at a time).\n\nConnect: mykaggle <compatible-url>",
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


# Register subcommands
session.register(app)
execution.register(app)
packages.register(app)


def main():
    app()


if __name__ == "__main__":
    main()
