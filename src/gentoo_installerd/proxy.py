"""Proxy settings for the installer's own downloads and for Portage inside the target.

The proxy comes from ``network.proxy`` (an ``http://host:port`` or ``https://host:port``
URL, optionally with ``user:password@``). Credentials are allowed but are never
written to the target's ``make.conf`` and never appear in logs.
"""

from __future__ import annotations

import urllib.request
from urllib.parse import SplitResult, urlsplit

NO_PROXY = "localhost,127.0.0.1,::1"
_URL_VARIABLES = ("http_proxy", "https_proxy", "ftp_proxy", "all_proxy")


def split_proxy(proxy: str) -> SplitResult:
    return urlsplit(proxy.strip())


def has_credentials(proxy: str) -> bool:
    parts = split_proxy(proxy)
    return bool(parts.username or parts.password)


def redact_proxy(proxy: str) -> str:
    """The proxy address with the password replaced, for logs and the review page."""
    if not proxy:
        return ""
    parts = split_proxy(proxy)
    if not parts.password:
        return proxy
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.username or ''}:<redacted>@{host}{port}"


def proxy_environment(proxy: str) -> dict[str, str]:
    """Environment variables that make wget, curl, git, rsync and Portage use the proxy."""
    if not proxy:
        return {}
    parts = split_proxy(proxy)
    environment: dict[str, str] = {}
    for name in _URL_VARIABLES:
        environment[name] = proxy
        environment[name.upper()] = proxy
    environment["no_proxy"] = environment["NO_PROXY"] = NO_PROXY
    if parts.hostname:
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
        environment["RSYNC_PROXY"] = f"{host}:{port}"
    return environment


def make_conf_settings(proxy: str) -> dict[str, str]:
    """Proxy variables that are safe to keep in the installed system's make.conf."""
    if not proxy or has_credentials(proxy):
        return {}
    return {"http_proxy": proxy, "https_proxy": proxy, "ftp_proxy": proxy}


def build_opener(proxy: str = "") -> urllib.request.OpenerDirector:
    """An opener for the installer's own HTTPS downloads (stage3 index, digests, archive)."""
    if not proxy:
        return urllib.request.build_opener()
    return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
