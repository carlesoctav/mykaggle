import logging
import os
import sys

import typer

from mykaggle.runtime import KaggleRuntime
from mykaggle.state import SessionState, StateStore


class State:
    def __init__(self):
        self.config_path = None
        self.logtostderr = False
        self._store = None

    @property
    def store(self):
        if self._store is None:
            self._store = StateStore(self.config_path)
        return self._store


state = State()


def setup_logging(log_to_stderr: bool):
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    requests_log = logging.getLogger("urllib3")
    requests_log.setLevel(logging.DEBUG)
    requests_log.propagate = True

    log_dir = os.path.expanduser("~/.config/mykaggle")
    os.makedirs(log_dir, exist_ok=True)
    file_handler = logging.FileHandler(os.path.join(log_dir, "mykaggle.log"))
    file_handler.setFormatter(logging.Formatter(log_format))
    logger.addHandler(file_handler)

    if log_to_stderr:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(logging.Formatter(log_format))
        logger.addHandler(stream_handler)


def resolve_session() -> SessionState:
    s = state.store.load()
    if s is None:
        typer.echo(
            "[mykaggle] Not connected. Run: mykaggle <compatible-url>", err=True
        )
        raise typer.Exit(1)
    return s


def make_runtime(s: SessionState) -> KaggleRuntime:
    """Build a runtime that writes healed kernel/session ids back to state."""

    def on_kernel_started(kernel_id: str):
        s.kernel_id = kernel_id
        state.store.save(s)

    def on_session_started(session_id: str):
        s.session_id = session_id
        state.store.save(s)

    return KaggleRuntime(
        url=s.url,
        kernel_id=s.kernel_id,
        session_id=s.session_id,
        on_kernel_started=on_kernel_started,
        on_session_started=on_session_started,
    )
