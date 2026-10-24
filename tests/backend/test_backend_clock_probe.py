"""proxy.mirror_clock_probe: the mirror's Date header, even when a wrong clock makes the certificate check fail."""

from __future__ import annotations

import socket
import ssl
import urllib.error
from unittest.mock import patch

from gentoo_installerd import proxy

NOW = 1_792_800_000.0
DATE = "Sun, 25 Oct 2026 00:00:00 GMT"  # 1792886400


class Opener:
    def __init__(self, date: str | None = DATE, error: BaseException | None = None) -> None:
        self.date, self.error, self.requests = date, error, []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        outer = self

        class Response:
            headers = {"Date": outer.date} if outer.date else {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return Response()


def probe(verified: Opener, unverified: Opener | None = None, proxy_url: str = ""):
    with patch.object(proxy, "build_opener", return_value=verified) as built, patch.object(proxy, "_unverified_opener", return_value=unverified or Opener()) as loose:
        result = proxy.mirror_clock_probe("https://distfiles.gentoo.org", proxy_url, now=lambda: NOW)
    return result, built, loose


def certificate_error() -> ssl.SSLCertVerificationError:
    return ssl.SSLCertVerificationError("certificate is not yet valid")


def test_a_working_connection_gives_the_skew_and_a_good_certificate_check():
    result, built, loose = probe(Opener())
    assert result == proxy.ClockProbe(skew=NOW - 1_792_886_400, tls_ok=True)
    built.assert_called_once_with("")
    loose.assert_not_called()


def test_the_probe_goes_through_the_configured_proxy():
    _, built, _ = probe(Opener(), proxy_url="http://proxy.example:3128")
    built.assert_called_once_with("http://proxy.example:3128")


def test_a_failed_certificate_check_still_measures_the_clock_and_reports_the_check_as_failed():
    # a PC whose clock battery died: the year is wrong, so every certificate is "not yet valid" or "expired"
    verified = Opener(error=urllib.error.URLError(certificate_error()))
    loose = Opener(date=DATE)
    result, _, built_loose = probe(verified, loose)
    assert result == proxy.ClockProbe(skew=NOW - 1_792_886_400, tls_ok=False)
    built_loose.assert_called_once_with("")
    assert len(loose.requests) == 1


def test_the_certificate_error_may_also_arrive_unwrapped():
    result, _, _ = probe(Opener(error=certificate_error()), Opener())
    assert result.tls_ok is False and result.skew is not None


def test_an_unreachable_mirror_says_nothing_and_is_not_retried_without_checks():
    for error in (urllib.error.URLError("unreachable"), socket.timeout(), TimeoutError(), ConnectionRefusedError()):
        result, _, loose = probe(Opener(error=error))
        assert result == proxy.ClockProbe(None, None), error
        loose.assert_not_called()


def test_when_even_the_unchecked_request_fails_the_check_failure_is_still_reported():
    result, _, _ = probe(Opener(error=certificate_error()), Opener(error=TimeoutError()))
    assert result == proxy.ClockProbe(None, False)


def test_a_missing_or_broken_date_header_gives_no_skew():
    assert probe(Opener(date=None))[0] == proxy.ClockProbe(None, True)
    assert probe(Opener(date="not a date"))[0] == proxy.ClockProbe(None, True)


def test_the_unchecked_opener_is_only_built_for_reading_a_date():
    opener = proxy._unverified_opener("http://proxy.example:3128")
    names = {type(handler).__name__ for handler in opener.handlers}
    assert {"HTTPSHandler", "ProxyHandler"} <= names
