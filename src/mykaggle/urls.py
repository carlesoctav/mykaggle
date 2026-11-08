"""Server URL handling for Kaggle 'compatible URLs'.

Two formats (same as JuypterSSH):
    https://host/...?token=ABC          token stripped, re-attached per request
    https://host/k/<id>/<jwt>/proxy      token stays in the path
"""

import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
import requests


def split_server_url(url: str):
    """Split into (base, token). Base never carries a query string."""
    parsed = urlparse(url.strip().rstrip("/"))
    token = None
    q = parse_qs(parsed.query)
    if q.get("token"):
        token = q["token"][0]
    base = urlunparse(parsed._replace(query="")).rstrip("/")
    return base, token


def resolve_jupyter_base(url: str) -> str:
    """Resolve the effective full base URL for Jupyter Server.

    If given a root proxy URL (e.g. https://.../?token=JWT), fetches
    /api/kernelspecs using ?token= to discover Kaggle's internal prefix
    (/k/<id>/<jwt>/proxy) so that libraries like jupyter_kernel_client
    work directly without 404s.
    """
    base, token = split_server_url(url)
    if "/k/" in base and base.endswith("/proxy"):
        return base
    if token:
        try:
            r = requests.get(f"{base}/api/kernelspecs?token={token}", timeout=10)
            if r.status_code == 200:
                ks = r.json()
                default_spec = ks.get("default", "python3")
                spec = ks.get("kernelspecs", {}).get(default_spec, {})
                logo = spec.get("resources", {}).get("logo-svg", "")
                if "/kernelspecs" in logo:
                    prefix = logo.split("/kernelspecs")[0]
                    parsed = urlparse(base)
                    return f"{parsed.scheme}://{parsed.netloc}{prefix}"
        except Exception:
            pass
    return base


def rest_url(base: str, token: str | None, path: str) -> str:
    url = base.rstrip("/") + "/" + path.lstrip("/")
    if token:
        url += "?" + urlencode({"token": token})
    return url


def ws_url(base: str, token: str | None, path: str) -> str:
    p = urlparse(rest_url(base, token, path))
    return urlunparse(p._replace(scheme="wss" if p.scheme == "https" else "ws"))


def redact(url: str) -> str:
    """Mask credentials in both URL shapes.

    >>> redact("https://h/api/x?token=ABC&other=1")
    'https://h/api/x?token=[REDACTED]&other=1'
    >>> redact("https://h/k/ID/JWT123/proxy/api/kernels")
    'https://h/k/<id>/[REDACTED]/proxy/api/kernels'
    >>> redact("https://h/plain/path")
    'https://h/plain/path'
    """
    url = re.sub(r"token=[^&\s'\"]+", "token=[REDACTED]", url)
    url = re.sub(r"/k/[^/\s'\"]+/[^/\s'\"]+/proxy", "/k/<id>/[REDACTED]/proxy", url)
    return url
