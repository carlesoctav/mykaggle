"""Kernel client over `jupyter_kernel_client` (same transport as colab-cli).

Auth mapping for Kaggle's two URL shapes:
    .../proxy          -> server_url=<full url>, token=None (JWT in path)
    ...?token=<jwt>    -> server_url=<base>, token=<jwt> (Bearer REST, ?token= WS)

Kernel attach policy stays mykaggle's own (verify stored id, else reuse
first live kernel, else spawn) so reconnects never disturb the notebook.
"""

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import jupyter_kernel_client
import requests

from mykaggle.net import friendly_error, is_retriable, request_with_retry
from mykaggle.urls import redact, rest_url, split_server_url

logger = logging.getLogger(__name__)


class KaggleRuntime:
    def __init__(
        self,
        url: str,
        kernel_id: Optional[str] = None,
        session_id: Optional[str] = None,
        on_kernel_started: Optional[Callable[[str], None]] = None,
        on_session_started: Optional[Callable[[str], None]] = None,
    ):
        self.server_url = url
        self.base, self.token = split_server_url(url)
        self.kernel_id = kernel_id
        self.session_id = session_id
        self.on_kernel_started = on_kernel_started
        self.on_session_started = on_session_started
        self._kernel_client = None
        # NOTE: the lib's websocket threads are non-daemon, so a connected
        # runtime MUST be stop()ed or the process hangs at exit. An atexit
        # guard cannot help (threading._shutdown runs before atexit
        # handlers), so every command path stops explicitly.

    # ------------------------------------------------------------------
    # kernel lifecycle (REST; attach-first)
    # ------------------------------------------------------------------
    def ensure_kernel(self) -> str:
        """Return a live kernel id, reusing the stored one when possible."""
        try:
            if self.kernel_id:
                r = request_with_retry(
                    "GET",
                    rest_url(self.base, self.token, f"/api/kernels/{self.kernel_id}"),
                    timeout=15,
                )
                if r.status_code == 200:
                    return self.kernel_id
                logger.info("Stored kernel %s is gone, re-attaching", self.kernel_id)

            kernels = request_with_retry(
                "GET", rest_url(self.base, self.token, "/api/kernels"), timeout=15
            ).json()
            if kernels:
                self.kernel_id = kernels[0]["id"]
            else:
                r = requests.post(
                    rest_url(self.base, self.token, "/api/kernels"),
                    json={"name": "python3"},
                    timeout=30,
                )
                r.raise_for_status()
                self.kernel_id = r.json()["id"]
        except requests.exceptions.RequestException as e:
            raise friendly_error(e) from e
        if self.on_kernel_started:
            self.on_kernel_started(self.kernel_id)
        return self.kernel_id

    @property
    def kernel_client(self):
        if not self._kernel_client:
            kid = self.ensure_kernel()
            client_kwargs = {
                "subprotocol": jupyter_kernel_client.JupyterSubprotocol.DEFAULT,
            }
            if self.session_id:
                # WSSession (Session) expects 'session' for the ID
                client_kwargs["session"] = self.session_id

            retries = 3
            backoff = 2
            for i in range(retries):
                try:
                    self._kernel_client = jupyter_kernel_client.KernelClient(
                        server_url=self.base,
                        token=self.token,
                        kernel_id=kid,
                        client_kwargs=client_kwargs,
                    )
                    # Force _own_kernel to False. This prevents the lib from
                    # automatically deleting the kernel when the client closes.
                    self._kernel_client._own_kernel = False
                    self._kernel_client.start()
                    break
                except requests.exceptions.RequestException as e:
                    if is_retriable(e) and i < retries - 1:
                        time.sleep(backoff ** (i + 1))
                        continue
                    raise friendly_error(e) from e

            if (
                not self.session_id
                and self._kernel_client._manager.client.session.session
            ):
                self.session_id = (
                    self._kernel_client._manager.client.session.session
                )
                if self.on_session_started:
                    self.on_session_started(self.session_id)

        return self._kernel_client

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------
    def execute_code(
        self,
        code: str,
        output_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
        timeout: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Execute code; timeout is an overall deadline (None waits forever).

        The lib's own timeout only bounds the final reply wait, so the
        deadline is enforced here: on expiry the kernel is interrupted
        and TimeoutError is raised.
        """
        from jupyter_kernel_client.client import output_hook as default_output_hook

        client = self.kernel_client  # ensures kernel
        outputs: List[Dict[str, Any]] = []

        def wrapped_output_hook(msg):
            new_indexes = default_output_hook(outputs, msg)
            if output_hook and new_indexes:
                for idx in sorted(new_indexes):
                    if idx < len(outputs):
                        output_hook(outputs[idx])

        result: Dict[str, Any] = {}

        def target():
            try:
                result["reply"] = client.execute_interactive(
                    code, output_hook=wrapped_output_hook, timeout=None
                )
            except BaseException as e:
                result["error"] = e

        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            try:
                requests.post(
                    rest_url(self.base, self.token, f"/api/kernels/{self.kernel_id}/interrupt"),
                    timeout=10,
                )
            except Exception:
                pass
            worker.join(10)
            raise TimeoutError(f"Execution timed out after {timeout} seconds")
        if "error" in result:
            e = result["error"]
            # Never leak the token (it may appear in handshake errors).
            raise RuntimeError(f"Kernel execution failed: {redact(str(e))}") from e

        reply = result.get("reply")
        reply_content = reply["content"] if reply else {"status": "error"}

        # If there's an error status but no error in outputs, synthesize one
        if reply_content.get("status") == "error":
            has_error_output = any(o.get("output_type") == "error" for o in outputs)
            if not has_error_output:
                outputs.append(
                    {
                        "output_type": "error",
                        "ename": reply_content.get("ename", "Error"),
                        "evalue": reply_content.get("evalue", "Unknown error"),
                        "traceback": reply_content.get("traceback", []),
                    }
                )

        return outputs

    def stop(self, shutdown_kernel: bool = False):
        if self._kernel_client:
            try:
                # We manage kernel lifecycle explicitly. Bypass the manager's
                # stop() and directly close channels and socket so a client
                # close can never shut the kernel down.
                client = self._kernel_client._manager.client
                client.stop_channels()
                if client.kernel_socket:
                    client.kernel_socket.close()

                if shutdown_kernel:
                    self._kernel_client._manager.shutdown_kernel(now=True)
            except Exception:
                logging.exception("Error stopping kernel client")
