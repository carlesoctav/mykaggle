"""Single-session state with multi-kernel registry.

mykaggle connects to one Kaggle server at a time, but can track multiple
named kernels for running background or foreground jobs.
"""

import json
import os
from typing import Dict, Optional

import filelock
from pydantic import BaseModel, Field


class KernelInfo(BaseModel):
    name: str
    kernel_id: str
    session_id: str
    started_at: str
    command: Optional[str] = None
    log_path: Optional[str] = None  # Remote path relative to /kaggle/working


class SessionState(BaseModel):
    url: str  # original compatible URL (carries the token or proxy path)
    kernel_id: Optional[str] = None  # primary / notebook kernel
    session_id: Optional[str] = None
    connected_at: Optional[str] = None
    kernel_slug: Optional[str] = None  # e.g. 'carlesoctav/mykaggle-batch-test'
    kernel_session_id: Optional[int] = None  # Kaggle VM session ID (/k/<id>/...)
    # Named kernels: name -> KernelInfo
    kernels: Dict[str, KernelInfo] = Field(default_factory=dict)


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
