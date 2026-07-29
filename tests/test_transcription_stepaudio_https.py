import http.client
import signal
import socket
import ssl
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationSource,
)
from video_auto_editor.runtime.errors import ErrorCode
from video_auto_editor.transcription._stepaudio_https import (
    StdlibStepAudioTransport,
)
from video_auto_editor.transcription.stepaudio import (
    StepAudioTransportFailure,
    StepAudioTransportFailureKind,
    StepAudioTransportRequest,
)

_ENDPOINT = "https://stepaudio.example.test/v1/audio/asr/sse"


class _FakeTlsContext:
    check_hostname = True
    verify_mode = ssl.CERT_REQUIRED

    @staticmethod
    def cert_store_stats():
        return {"x509_ca": 1}


class _FakeSocket:
    def __init__(self, closed: Event) -> None:
        self.closed = closed
        self.timeouts: list[float] = []

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def shutdown(self, _how: int) -> None:
        self.closed.set()


class _FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        read_failure: Exception | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self._offset = 0
        self._headers = headers or {}
        self._read_failure = read_failure
        self.read_calls = 0
        self.closed = False

    def getheader(self, name: str, default=None):
        return self._headers.get(name, default)

    def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        if self._read_failure is not None:
            raise self._read_failure
        if size < 0:
            size = len(self._body) - self._offset
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class _FakeConnection:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float,
        context,
        response: _FakeResponse,
        connect_failure: Exception | None = None,
        write_failure: Exception | None = None,
        read_failure: Exception | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.response = response
        self.connect_failure = connect_failure
        self.write_failure = write_failure
        self.read_failure = read_failure
        self.closed = Event()
        self.sock = _FakeSocket(self.closed)
        self.connected = False
        self.tunnel = None
        self.request_line = None
        self.headers: list[tuple[str, str]] = []
        self.body = None

    def set_tunnel(self, host: str, port: int, headers=None) -> None:
        self.tunnel = (host, port, dict(headers or {}))

    def connect(self) -> None:
        if self.connect_failure is not None:
            raise self.connect_failure
        self.connected = True

    def putrequest(
        self,
        method: str,
        selector: str,
        *,
        skip_host: bool,
        skip_accept_encoding: bool,
    ) -> None:
        self.request_line = (
            method,
            selector,
            skip_host,
            skip_accept_encoding,
        )

    def putheader(self, name: str, value: str) -> None:
        self.headers.append((name, value))

    def endheaders(self, body: bytes) -> None:
        if self.write_failure is not None:
            raise self.write_failure
        self.body = body

    def getresponse(self):
        if self.read_failure is not None:
            raise self.read_failure
        return self.response

    def close(self) -> None:
        self.closed.set()


class _ConnectionFactory:
    def __init__(
        self,
        response: _FakeResponse,
        *,
        connect_failure: Exception | None = None,
        write_failure: Exception | None = None,
        read_failure: Exception | None = None,
    ) -> None:
        self.response = response
        self.connect_failure = connect_failure
        self.write_failure = write_failure
        self.read_failure = read_failure
        self.connections: list[_FakeConnection] = []

    def __call__(
        self,
        host: str,
        port: int,
        *,
        timeout: float,
        context,
    ) -> _FakeConnection:
        connection = _FakeConnection(
            host,
            port,
            timeout=timeout,
            context=context,
            response=self.response,
            connect_failure=self.connect_failure,
            write_failure=self.write_failure,
            read_failure=self.read_failure,
        )
        self.connections.append(connection)
        return connection


def _request(
    *,
    credential: str = "credential-canary",
    body: bytes = b'{"audio":"body-canary"}',
) -> StepAudioTransportRequest:
    return StepAudioTransportRequest(
        endpoint=_ENDPOINT,
        credential=credential,
        body=body,
        timeout_seconds=3,
    )


def _transport(
    factory: _ConnectionFactory,
    *,
    proxies=None,
    bypass=False,
    max_response_bytes: int = 4 * 1024 * 1024,
) -> StdlibStepAudioTransport:
    return StdlibStepAudioTransport(
        _ENDPOINT,
        _connection_factory=factory,
        _tls_context_factory=_FakeTlsContext,
        _proxy_getter=lambda: dict(proxies or {}),
        _proxy_bypass=lambda _host: bypass,
        _max_response_bytes=max_response_bytes,
    )


def test_readiness_uses_verified_system_tls_without_honoring_ssl_key_log(
    monkeypatch,
    tmp_path,
):
    key_log = tmp_path / "must-not-be-created.keys"
    monkeypatch.setenv("SSLKEYLOGFILE", str(key_log))
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setenv("NO_PROXY", "*")

    transport = StdlibStepAudioTransport(_ENDPOINT)

    assert transport.check_readiness() == ()
    assert transport.check_readiness() == ()
    assert not key_log.exists()


def test_readiness_reports_invalid_https_proxy_without_disclosing_value():
    secret_proxy = "https://proxy-user:proxy-secret@proxy.example.test:8443"
    transport = StdlibStepAudioTransport(
        _ENDPOINT,
        _tls_context_factory=_FakeTlsContext,
        _proxy_getter=lambda: {"https": secret_proxy},
        _proxy_bypass=lambda _host: False,
    )

    issues = transport.check_readiness()

    assert len(issues) == 1
    assert issues[0].error_code is ErrorCode.CONFIG_VALUE_INVALID
    assert issues[0].diagnostics == {
        "field": "environment.https_proxy",
        "reason_code": "value.invalid_format",
    }
    assert secret_proxy not in repr(issues)
    assert "proxy-secret" not in repr(transport)


def test_send_uses_fixed_request_whitelist_and_returns_bounded_response():
    response = _FakeResponse(
        b"data: [DONE]\n\n",
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Content-Length": "14",
            "X-Request-Id": "remote-request-canary",
        },
    )
    factory = _ConnectionFactory(response)
    transport = _transport(factory)
    request = _request()
    cancellation = CancellationSource().token

    returned = transport.send(request, cancellation)

    connection = factory.connections[0]
    assert connection.host == "stepaudio.example.test"
    assert connection.port == 443
    assert connection.connected is True
    assert connection.tunnel is None
    assert connection.request_line == (
        "POST",
        "/v1/audio/asr/sse",
        True,
        True,
    )
    assert dict(connection.headers) == {
        "Host": "stepaudio.example.test",
        "Authorization": "Bearer credential-canary",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Content-Length": str(len(request.body)),
        "Connection": "close",
    }
    assert connection.body == request.body
    assert returned.status_code == 200
    assert returned.content_type == "text/event-stream; charset=utf-8"
    assert returned.body == b"data: [DONE]\n\n"
    assert returned.remote_request_id == "remote-request-canary"
    assert connection.sock.timeouts
    assert all(
        0 < timeout <= request.timeout_seconds
        for timeout in connection.sock.timeouts
    )
    assert response.closed is True
    assert connection.closed.is_set()


def test_send_honors_https_proxy_with_connect_and_keeps_proxy_auth_out_of_origin():
    response = _FakeResponse(
        b"",
        status=401,
        headers={"Content-Type": "application/json"},
    )
    factory = _ConnectionFactory(response)
    transport = _transport(
        factory,
        proxies={
            "https": "http://proxy-user:proxy-secret@proxy.example.test:8080"
        },
    )

    returned = transport.send(_request(), CancellationSource().token)

    connection = factory.connections[0]
    assert (connection.host, connection.port) == (
        "proxy.example.test",
        8080,
    )
    assert connection.tunnel is not None
    tunnel_host, tunnel_port, tunnel_headers = connection.tunnel
    assert (tunnel_host, tunnel_port) == (
        "stepaudio.example.test",
        443,
    )
    assert tunnel_headers["Host"] == "stepaudio.example.test:443"
    assert tunnel_headers["Proxy-Authorization"].startswith("Basic ")
    assert "Proxy-Authorization" not in dict(connection.headers)
    assert returned.status_code == 401
    assert returned.body == b""
    assert response.read_calls == 0


def test_send_honors_no_proxy_for_https_endpoint():
    response = _FakeResponse(b"data: [DONE]\n\n")
    factory = _ConnectionFactory(response)
    transport = _transport(
        factory,
        proxies={"https": "http://proxy.example.test:8080"},
        bypass=True,
    )

    transport.send(_request(), CancellationSource().token)

    connection = factory.connections[0]
    assert (connection.host, connection.port) == (
        "stepaudio.example.test",
        443,
    )
    assert connection.tunnel is None


@pytest.mark.parametrize(
    ("endpoint", "bare_host", "host_header"),
    [
        (
            "https://stepaudio.example.test:8443/v1/audio/asr/sse",
            "stepaudio.example.test",
            "stepaudio.example.test:8443",
        ),
        (
            "https://[2001:db8::1]:8443/v1/audio/asr/sse",
            "2001:db8::1",
            "[2001:db8::1]:8443",
        ),
    ],
)
def test_send_checks_no_proxy_with_bare_host(
    endpoint,
    bare_host,
    host_header,
):
    response = _FakeResponse(b"data: [DONE]\n\n")
    factory = _ConnectionFactory(response)
    checked_hosts = []

    def bypass(host):
        checked_hosts.append(host)
        return host == bare_host

    transport = StdlibStepAudioTransport(
        endpoint,
        _connection_factory=factory,
        _tls_context_factory=_FakeTlsContext,
        _proxy_getter=lambda: {
            "https": "http://proxy.example.test:8080"
        },
        _proxy_bypass=bypass,
    )
    request = StepAudioTransportRequest(
        endpoint=endpoint,
        credential="credential-canary",
        body=b'{"audio":"body-canary"}',
        timeout_seconds=3,
    )

    transport.send(request, CancellationSource().token)

    connection = factory.connections[0]
    assert checked_hosts == [bare_host]
    assert (connection.host, connection.port) == (bare_host, 8443)
    assert connection.tunnel is None
    assert dict(connection.headers)["Host"] == host_header


@pytest.mark.parametrize(
    ("native_failure", "expected_kind"),
    [
        (
            socket.gaierror("dns-canary-must-not-leak"),
            StepAudioTransportFailureKind.DNS_FAILED,
        ),
        (
            ssl.SSLError("tls-canary-must-not-leak"),
            StepAudioTransportFailureKind.TLS_FAILED,
        ),
        (
            TimeoutError("timeout-canary-must-not-leak"),
            StepAudioTransportFailureKind.CONNECT_TIMEOUT,
        ),
        (
            OSError("connection-canary-must-not-leak"),
            StepAudioTransportFailureKind.CONNECTION_FAILED,
        ),
    ],
)
def test_connect_failures_are_stable_and_do_not_leak_native_text(
    native_failure,
    expected_kind,
):
    factory = _ConnectionFactory(
        _FakeResponse(b""),
        connect_failure=native_failure,
    )
    transport = _transport(factory)

    with pytest.raises(StepAudioTransportFailure) as raised:
        transport.send(_request(), CancellationSource().token)

    assert raised.value.kind is expected_kind
    rendered = repr(raised.value) + str(raised.value)
    assert "canary-must-not-leak" not in rendered
    assert "credential-canary" not in rendered


@pytest.mark.parametrize(
    ("factory_kwargs", "expected_kind"),
    [
        (
            {"write_failure": TimeoutError("write-canary")},
            StepAudioTransportFailureKind.WRITE_TIMEOUT,
        ),
        (
            {"read_failure": TimeoutError("read-canary")},
            StepAudioTransportFailureKind.READ_TIMEOUT,
        ),
        (
            {"read_failure": http.client.BadStatusLine("protocol-canary")},
            StepAudioTransportFailureKind.RESPONSE_PROTOCOL_INVALID,
        ),
    ],
)
def test_request_phase_failures_are_stably_classified(
    factory_kwargs,
    expected_kind,
):
    factory = _ConnectionFactory(_FakeResponse(b""), **factory_kwargs)
    transport = _transport(factory)

    with pytest.raises(StepAudioTransportFailure) as raised:
        transport.send(_request(), CancellationSource().token)

    assert raised.value.kind is expected_kind
    assert "canary" not in (repr(raised.value) + str(raised.value))


def test_incomplete_and_oversized_responses_are_bounded_and_stable():
    incomplete = _FakeResponse(
        b"",
        read_failure=http.client.IncompleteRead(
            b"partial-canary",
            99,
        ),
    )
    incomplete_transport = _transport(_ConnectionFactory(incomplete))
    with pytest.raises(StepAudioTransportFailure) as raised:
        incomplete_transport.send(_request(), CancellationSource().token)
    assert (
        raised.value.kind
        is StepAudioTransportFailureKind.RESPONSE_TRUNCATED
    )
    assert "partial-canary" not in repr(raised.value)

    oversized = _FakeResponse(
        b"response-body-canary",
        headers={"Content-Length": "20"},
    )
    oversized_transport = _transport(
        _ConnectionFactory(oversized),
        max_response_bytes=10,
    )
    with pytest.raises(StepAudioTransportFailure) as raised:
        oversized_transport.send(_request(), CancellationSource().token)
    assert (
        raised.value.kind
        is StepAudioTransportFailureKind.RESPONSE_TOO_LARGE
    )
    assert oversized.read_calls == 0


class _BlockingConnection(_FakeConnection):
    def getresponse(self):
        self.response_started.set()
        self.closed.wait(timeout=5)
        raise OSError("closed transport")

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.response_started = Event()


class _BlockingFactory(_ConnectionFactory):
    def __call__(self, host, port, *, timeout, context):
        connection = _BlockingConnection(
            host,
            port,
            timeout=timeout,
            context=context,
            response=self.response,
        )
        self.connections.append(connection)
        return connection


class _SiblingCancellation(Exception):
    pass


class _CombinedCancellationView:
    def __init__(self) -> None:
        self.event = Event()

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    @property
    def signal_number(self):
        return None

    def wait(self, timeout=None):
        return self.event.wait(timeout)

    def raise_if_cancelled(self):
        if self.cancelled:
            raise _SiblingCancellation()


def test_root_cancellation_aborts_inflight_response_read():
    factory = _BlockingFactory(_FakeResponse(b""))
    transport = _transport(factory)
    cancellation = CancellationSource()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            transport.send,
            _request(),
            cancellation.token,
        )
        assert factory.connections[0].response_started.wait(timeout=1)
        cancellation.request(signal.SIGTERM)
        with pytest.raises(CancellationRequested):
            future.result(timeout=1)

    assert factory.connections[0].closed.is_set()


def test_combined_cancellation_aborts_inflight_response_read():
    factory = _BlockingFactory(_FakeResponse(b""))
    transport = _transport(factory)
    cancellation = _CombinedCancellationView()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            transport.send,
            _request(),
            cancellation,
        )
        assert factory.connections[0].response_started.wait(timeout=1)
        cancellation.event.set()
        with pytest.raises(_SiblingCancellation):
            future.result(timeout=1)

    assert factory.connections[0].closed.is_set()
