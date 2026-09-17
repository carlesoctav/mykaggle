import base64
from urllib.parse import quote

import requests

from mykaggle.net import friendly_error, request_with_retry
from mykaggle.urls import rest_url, split_server_url

# Kaggle serves the Contents API from /kaggle/working. The kernel's cwd can
# be anything (notebook cells change it), so generated %run / -r references
# must use this absolute root, never a bare name.
KAGGLE_WORKING_DIR = "/kaggle/working"


class ContentsClient:
    def __init__(self, server_url: str):
        self.base, self.token = split_server_url(server_url)

    def _request(self, method: str, path: str, json_data: dict = None):
        # Quote the path, but don't encode slashes so directory paths stay intact
        quoted_path = quote(path.strip("/"), safe="/")
        url = rest_url(self.base, self.token, f"/api/contents/{quoted_path}")
        try:
            response = request_with_retry(method, url, json=json_data, timeout=60)
        except requests.exceptions.RequestException as e:
            raise friendly_error(e) from e

        if response.status_code == 404:
            raise FileNotFoundError(f"File or directory not found: {path}")

        try:
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise friendly_error(e) from e

        if method == "DELETE":
            return None

        return response.json()

    def list_dir(self, path: str):
        return self._request("GET", path)

    def upload(self, local_path: str, remote_path: str):
        with open(local_path, "rb") as f:
            content = f.read()

        b64_content = base64.b64encode(content).decode("ascii")
        filename = remote_path.split("/")[-1]

        payload = {
            "name": filename,
            "path": remote_path,
            "type": "file",
            "format": "base64",
            "content": b64_content,
            "chunk": 1,
        }

        return self._request("PUT", remote_path, json_data=payload)

    def download(self, remote_path: str, local_path: str):
        data = self._request("GET", remote_path)

        if data.get("type") == "directory":
            raise IsADirectoryError(f"Cannot download a directory: {remote_path}")

        content = data.get("content", "")
        fmt = data.get("format")

        if fmt == "base64":
            content_bytes = base64.b64decode(content)
        else:
            content_bytes = str(content).encode("utf-8")

        with open(local_path, "wb") as f:
            f.write(content_bytes)
