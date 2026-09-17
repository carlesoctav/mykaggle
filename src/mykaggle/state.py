"""Single-session state. mykaggle connects to one server at a time."""

import json
import os
from typing import Optional

import filelock
from pydantic import BaseModel


class SessionState(BaseModel):
    url: str  # original compatible URL (carries the token)
    kernel_id: Optional[str] = None
    session_id: Optional[str] = None
    connected_at: Optional[str] = None


class StateStore:
    def __init__(self, path: Optional[str] = None):
        if not path:
            path = os.path.expanduser("~/.config/mykaggle/session.json")
        self.path = path
        self._lock = filelock.FileLock("%s.lock" % self.path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def load(self) -> Optional[SessionState]:
        with self._lock:
            if not os.path.exists(self.path):
                return None
            with open(self.path) as f:
                content = f.read()
            if not content or content.isspace():
                return None
            try:
                return SessionState.model_validate(json.loads(content))
            except Exception:
                return None

    def save(self, state: SessionState):
        tmp = "%s.tmp" % self.path
        with self._lock:
            with open(tmp, "w") as f:
                f.write(state.model_dump_json(indent=2))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)

    def clear(self):
        with self._lock:
            if os.path.exists(self.path):
                os.remove(self.path)
