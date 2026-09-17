"""Server URL handling for Kaggle 'compatible URLs'.

Two formats (same as JuypterSSH):
    https://host/...?token=ABC          token stripped, re-attached per request
    https://host/k/<id>/<jwt>/proxy      token stays in the path
"""

import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


def split_server_url(url):
    """Split into (base, token). Base never carries a query string."""
    parsed = urlparse(url.strip().rstrip("/"))
    token = None
    q = parse_qs(parsed.query)
    if q.get("token"):
        token = q["token"][0]
    base = urlunparse(parsed._replace(query="")).rstrip("/")
    return base, token


def rest_url(base, token, path):
    url = base + path
    if token:
        url += "?" + urlencode({"token": token})
    return url


def ws_url(base, token, path):
    p = urlparse(rest_url(base, token, path))
    return urlunparse(p._replace(scheme="wss" if p.scheme == "https" else "ws"))


def redact(url):
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
