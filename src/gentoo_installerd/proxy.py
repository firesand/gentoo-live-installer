"""Proxy settings for the installer's own downloads and for Portage inside the target.

The proxy comes from ``network.proxy`` (an ``http://host:port`` or ``https://host:port``
URL, optionally with ``user:password@``). Credentials are allowed but are never
written to the target's ``make.conf`` and never appear in logs.
"""

from __future__ import annotations

import email.utils
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable
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


def make_conf_settings(proxy: str, keep: bool = True) -> dict[str, str]:
    """Proxy variables that are safe to keep in the installed system's make.conf.

    Nothing is kept when the proxy has credentials (they never reach the installed system) or when ``keep`` is False
    (``network.keep_proxy``: the proxy is for the installation only).
    """
    if not proxy or not keep or has_credentials(proxy):
        return {}
    return {"http_proxy": proxy, "https_proxy": proxy, "ftp_proxy": proxy}


def build_opener(proxy: str = "") -> urllib.request.OpenerDirector:
    """An opener for the installer's own HTTPS downloads (stage3 index, digests, archive)."""
    if not proxy:
        return urllib.request.build_opener()
    return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


def mirror_clock_skew(mirror: str, proxy: str = "", *, timeout: float = 10.0,
                      now: Callable[[], float] = time.time) -> float | None:
    """Seconds this computer's clock is ahead (+) of or behind (-) the mirror; None when unknown.

    TLS certificates and the stage3 signature checks fail confusingly on a machine whose clock is
    off (a dead RTC battery), so the daemon's preflight asks the mirror for the time first.
    """
    request = urllib.request.Request(mirror.rstrip("/") + "/", method="HEAD", headers={"User-Agent": "gentoo-live-installer/0.1"})
    try:
        before = now()
        with build_opener(proxy).open(request, timeout=timeout) as response:
            header = response.headers.get("Date", "")
        parsed = email.utils.parsedate_to_datetime(header) if header else None
    except (urllib.error.URLError, OSError, ValueError, TypeError):
        return None
    if parsed is None:
        return None
    return before - parsed.timestamp()


@dataclass(frozen=True)
class ClockProbe:
    """What the mirror's HTTP Date header says about this computer's clock."""

    skew: float | None  # seconds this clock is ahead (+) of or behind (-) the mirror; None when unknown
    tls_ok: bool | None  # False when the certificate check failed (a clock that is far off does that); None when unknown


def _is_certificate_problem(error: BaseException) -> bool:
    reason = getattr(error, "reason", error)
    return isinstance(reason, ssl.SSLCertVerificationError) or isinstance(error, ssl.SSLCertVerificationError)


def _unverified_opener(proxy: str) -> urllib.request.OpenerDirector:
    """An opener that does not check the certificate. Only for reading a Date header; nothing it returns is trusted."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    handlers: list[urllib.request.BaseHandler] = [urllib.request.HTTPSHandler(context=context)]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def mirror_clock_probe(mirror: str, proxy: str = "", *, timeout: float = 10.0,
                       now: Callable[[], float] = time.time) -> ClockProbe:
    """Like mirror_clock_skew, and it still measures the clock when the certificate check fails.

    A PC whose clock battery died (it starts in the year 2000, or the year the firmware was made)
    cannot verify any HTTPS certificate, which is exactly when the clock has to be reported.
    """
    request = urllib.request.Request(mirror.rstrip("/") + "/", method="HEAD", headers={"User-Agent": "gentoo-live-installer/0.1"})
    tls_ok = True
    opener = build_opener(proxy)
    while True:
        try:
            before = now()
            with opener.open(request, timeout=timeout) as response:
                header = response.headers.get("Date", "")
            break
        except (urllib.error.URLError, OSError, ValueError, TypeError) as error:
            if tls_ok and _is_certificate_problem(error):
                tls_ok = False  # measure again without the check; only the Date header is used
                opener = _unverified_opener(proxy)
                continue
            return ClockProbe(None, None if tls_ok else False)
    try:
        parsed = email.utils.parsedate_to_datetime(header) if header else None
    except (ValueError, TypeError):
        parsed = None
    return ClockProbe(None if parsed is None else before - parsed.timestamp(), tls_ok)
