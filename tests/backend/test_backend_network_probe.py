"""The connectivity check through a proxy that asks for a login."""

from __future__ import annotations

import base64
import socket
import threading

import pytest

from gentoo_installerd.inspect import _network_ready


class Proxy:
    """A one-connection-at-a-time proxy that says 200 to a CONNECT with the right login and 407 to anything else."""

    def __init__(self, login: str | None) -> None:
        self.login = login
        self.requests: list[str] = []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.listener.settimeout(5)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self) -> None:
        try:
            while True:
                connection, _ = self.listener.accept()
                with connection:
                    connection.settimeout(2)
                    data = b""
                    while b"\r\n\r\n" not in data:
                        chunk = connection.recv(1024)
                        if not chunk:
                            break
                        data += chunk
                    text = data.decode()
                    self.requests.append(text)
                    wanted = "Proxy-Authorization: Basic " + base64.b64encode(self.login.encode()).decode() if self.login else ""
                    ok = not self.login or wanted in text
                    connection.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n" if ok else b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
        except OSError:
            pass

    def close(self) -> None:
        self.listener.close()


@pytest.fixture
def proxy():
    made: list[Proxy] = []

    def make(login: str | None) -> Proxy:
        made.append(Proxy(login))
        return made[-1]

    yield make
    for item in made:
        item.close()


def test_a_proxy_that_needs_no_login_works_as_before(proxy):
    server = proxy(None)
    assert _network_ready("distfiles.gentoo.org", 443, f"http://127.0.0.1:{server.port}", timeout=2)
    assert server.requests[0].startswith("CONNECT distfiles.gentoo.org:443 HTTP/1.1") and "Proxy-Authorization" not in server.requests[0]


def test_the_login_in_the_address_is_sent_with_the_connect_request(proxy):
    server = proxy("alice:p@ss word")
    assert _network_ready("distfiles.gentoo.org", 443, f"http://alice:p%40ss%20word@127.0.0.1:{server.port}", timeout=2)


def test_a_missing_or_wrong_login_is_still_no_network(proxy):
    server = proxy("alice:secret")
    assert not _network_ready("distfiles.gentoo.org", 443, f"http://127.0.0.1:{server.port}", timeout=2)
    assert not _network_ready("distfiles.gentoo.org", 443, f"http://alice:wrong@127.0.0.1:{server.port}", timeout=2)


def test_a_user_name_without_a_password_is_sent_with_an_empty_password(proxy):
    server = proxy("alice:")
    assert _network_ready("distfiles.gentoo.org", 443, f"http://alice@127.0.0.1:{server.port}", timeout=2)


def test_nothing_listening_is_just_false():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    assert _network_ready("distfiles.gentoo.org", 443, f"http://127.0.0.1:{port}", timeout=1) is False
