"""Resilient HTTP for talking to the server: retries + friendly errors."""

import time

import requests

from mykaggle.urls import redact

_RETRIABLE = (
    requests.exceptions.ConnectionError,  # DNS blips, refused, reset
    requests.exceptions.ReadTimeout,
    requests.exceptions.ConnectTimeout,
)


def is_retriable(exc):
    """Network blips worth retrying (never auth/server rejections).

    >>> is_retriable(requests.exceptions.ConnectTimeout())
    True
    >>> is_retriable(requests.exceptions.ConnectionError())
    True
    >>> is_retriable(requests.exceptions.HTTPError())
    False
    >>> is_retriable(ValueError())
    False
    """
    return isinstance(exc, _RETRIABLE)


def friendly_error(e):
    """Short user-facing error with credentials masked."""
    status = getattr(getattr(e, "response", None), "status_code", None)
    if status in (401, 403):
        return RuntimeError(
            f"Server rejected credentials (HTTP {status}); session expired? "
            "Reconnect with: mykaggle <compatible-url>"
        )
    if status:
        return RuntimeError(f"Server request failed (HTTP {status}): {redact(str(e))}")
    return RuntimeError(f"Cannot reach server (network/DNS?): {redact(str(e))}")


def request_with_retry(method, url, *, retries=3, backoff=2, **kwargs):
    """requests.request with backoff on retriable network blips."""
    for i in range(retries):
        try:
            return requests.request(method, url, **kwargs)
        except requests.exceptions.RequestException as e:
            if is_retriable(e) and i < retries - 1:
                time.sleep(backoff ** (i + 1))
                continue
            raise
