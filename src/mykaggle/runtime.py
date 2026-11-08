"""Kernel client over `jupyter_kernel_client` (same transport as colab-cli).

Auth mapping for Kaggle's two URL shapes:
    .../proxy          -> server_url=<full url>, token=None (JWT in path)
    ...?token=<jwt>    -> resolved to internal proxy URL or server_url=<base>, token=<jwt>
"""

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import jupyter_kernel_client
import requests

from mykaggle.net import friendly_error, is_retriable, request_with_retry
from mykaggle.urls import redact, resolve_jupyter_base, rest_url, split_server_url

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
        self.effective_base = resolve_jupyter_base(url)
        self.base, self.token = split_server_url(self.effective_base)
        self.kernel_id = kernel_id
        self.session_id = session_id
        self.on_kernel_started = on_kernel_started
        self.on_session_started = on_session_started
        self._kernel_client = None

    # ------------------------------------------------------------------
    # kernel lifecycle (REST)
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

    def spawn_new_kernel(self) -> str:
        """Always spawn a fresh independent kernel."""
        try:
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

    def list_kernels(self) -> List[Dict[str, Any]]:
        """List all active kernels on the server."""
        try:
            r = request_with_retry(
                "GET", rest_url(self.base, self.token, "/api/kernels"), timeout=15
            )
            return r.json()
        except requests.exceptions.RequestException as e:
            raise friendly_error(e) from e

    def interrupt(self) -> None:
        """Send SIGINT to current kernel."""
        if not self.kernel_id:
            return
        requests.post(
            rest_url(self.base, self.token, f"/api/kernels/{self.kernel_id}/interrupt"),
            timeout=10,
        )

    def restart(self) -> None:
        """Restart current kernel."""
        if not self.kernel_id:
            return
        requests.post(
            rest_url(self.base, self.token, f"/api/kernels/{self.kernel_id}/restart"),
            timeout=15,
        )

    def delete_kernel(self, kernel_id: Optional[str] = None) -> None:
        """Delete / stop kernel (204)."""
        kid = kernel_id or self.kernel_id
        if not kid:
            return
        requests.delete(
            rest_url(self.base, self.token, f"/api/kernels/{kid}"),
            timeout=15,
        )

    @property
    def kernel_client(self):
        if not self._kernel_client:
            kid = self.kernel_id or self.ensure_kernel()
            client_kwargs = {
                "subprotocol": jupyter_kernel_client.JupyterSubprotocol.DEFAULT,
            }
            if self.session_id:
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
    def execute_detached(self, code: str) -> str:
        """Fire an execute request without waiting for results."""
        client = self.kernel_client
        session = client._manager.client.session
        stream = client._manager.client.kernel_socket
        msg = session.send(
            stream, "shell", "execute_request", {"code": code, "silent": False}
        )
        return msg["header"]["msg_id"]

    def execute_code(
        self,
        code: str,
        output_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
        timeout: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Execute code in foreground; stream output and handle Ctrl+C."""
        from jupyter_kernel_client.client import output_hook as default_output_hook

        client = self.kernel_client
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
                self.interrupt()
            except Exception:
                pass
            worker.join(10)
            raise TimeoutError(f"Execution timed out after {timeout} seconds")
        if "error" in result:
            e = result["error"]
            raise RuntimeError(f"Kernel execution failed: {redact(str(e))}") from e

        reply = result.get("reply")
        reply_content = reply["content"] if reply else {"status": "error"}

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
                client = self._kernel_client._manager.client
                client.stop_channels()
                if client.kernel_socket:
                    client.kernel_socket.close()

                if shutdown_kernel and self.kernel_id:
                    self.delete_kernel(self.kernel_id)
            except Exception:
                logging.exception("Error stopping kernel client")
            finally:
                self._kernel_client = None
