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
from video_auto_editor.text_model._stepfun_https import (
    StdlibStepFunTransport,
)
from video_auto_editor.text_model.interface import TextModelReadinessCode
from video_auto_editor.text_model.stepfun import (
    StepFunTransportFailure,
    StepFunTransportFailureKind,
    StepFunTransportRequest,
)

_ENDPOINT = "https://stepfun.example.test/v1/chat/completions"
_CREDENTIAL = "credential-canary"
_BODY = b'{"messages":[{"content":"body-canary"}]}'
_PROXY_SECRET = "proxy-secret-canary"


class _FakeTlsContext:
    check_hostname = True
    verify_mode = ssl.CERT_REQUIRED

    @staticmethod
    def cert_store_stats():
        return {"x509_ca": 1}


class _InsecureTlsContext:
    check_hostname = False
    verify_mode = ssl.CERT_NONE

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
        self.read_sizes: list[int] = []
        self.closed = False

    def getheader(self, name: str, default=None):
        return self._headers.get(name, default)

    def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        self.read_sizes.append(size)
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
    connection_type = _FakeConnection

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
        self.created = Event()

    def __call__(
        self,
        host: str,
        port: int,
        *,
        timeout: float,
        context,
    ) -> _FakeConnection:
        connection = self.connection_type(
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
        self.created.set()
        return connection


def _request(
    *,
    headers: dict[str, str] | None = None,
    credential: str = _CREDENTIAL,
    body: bytes = _BODY,
) -> StepFunTransportRequest:
    return StepFunTransportRequest(
        endpoint=_ENDPOINT,
        credential=credential,
        headers=dict(headers or {}),
        body=body,
        timeout_seconds=3,
    )


def _transport(
    factory: _ConnectionFactory,
    *,
    proxies=None,
    bypass=False,
    max_response_bytes: int = 4 * 1024 * 1024,
) -> StdlibStepFunTransport:
    return StdlibStepFunTransport(
        _ENDPOINT,
        _connection_factory=factory,
        _tls_context_factory=_FakeTlsContext,
        _proxy_getter=lambda: dict(proxies or {}),
        _proxy_bypass=lambda _host: bypass,
        _max_response_bytes=max_response_bytes,
    )


def _assert_no_sensitive_text(value: object) -> None:
    rendered = repr(value) + str(value)
    assert _CREDENTIAL not in rendered
    assert "body-canary" not in rendered
    assert _PROXY_SECRET not in rendered


def test_system_tls_is_verified_and_does_not_honor_ssl_key_log(
    monkeypatch,
    tmp_path,
):
    key_log = tmp_path / "must-not-be-created.keys"
    monkeypatch.setenv("SSLKEYLOGFILE", str(key_log))
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    response = _FakeResponse(
        b'{"choices":[]}',
        headers={
            "Content-Type": "application/json",
            "Content-Length": "14",
        },
    )
    factory = _ConnectionFactory(response)
    transport = StdlibStepFunTransport(
        _ENDPOINT,
        _connection_factory=factory,
    )

    assert transport.check_readiness() == ()
    transport.send(_request(), CancellationSource().token)

    context = factory.connections[0].context
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert getattr(context, "keylog_filename", None) is None
    assert not key_log.exists()


def test_readiness_reports_non_https_endpoint_without_disclosing_it():
    endpoint = "http://endpoint-secret.example.test/v1/chat/completions"
    transport = StdlibStepFunTransport(
        endpoint,
        _tls_context_factory=_FakeTlsContext,
        _proxy_getter=dict,
    )

    issues = transport.check_readiness()

    assert len(issues) == 1
    assert issues[0].code is TextModelReadinessCode.HTTPS_REQUIRED
    assert issues[0].diagnostics == {"field": "text_model_provider_config.endpoint"}
    assert endpoint not in repr(issues)
    assert endpoint not in repr(transport)


def test_readiness_rejects_tls_context_without_hostname_verification():
    transport = StdlibStepFunTransport(
        _ENDPOINT,
        _tls_context_factory=_InsecureTlsContext,
        _proxy_getter=dict,
        _proxy_bypass=lambda _host: True,
    )

    issues = transport.check_readiness()

    assert len(issues) == 1
    assert issues[0].code is TextModelReadinessCode.TLS_VERIFICATION_UNAVAILABLE
    assert issues[0].diagnostics == {
        "component": "tls_ca",
        "operation": "tls.load_ca",
        "reason_code": "tls.verification_unavailable",
    }


def test_send_refuses_tls_context_without_certificate_and_hostname_verification():
    response = _FakeResponse(
        b'{"choices":[]}',
        headers={
            "Content-Type": "application/json",
            "Content-Length": "14",
        },
    )
    factory = _ConnectionFactory(response)
    transport = StdlibStepFunTransport(
        _ENDPOINT,
        _connection_factory=factory,
        _tls_context_factory=_InsecureTlsContext,
        _proxy_getter=dict,
        _proxy_bypass=lambda _host: True,
    )

    with pytest.raises(StepFunTransportFailure) as captured:
        transport.send(
            _request(),
            CancellationSource(clock=lambda: 0.0).token,
        )

    assert captured.value.kind is StepFunTransportFailureKind.TLS_FAILED
    assert factory.connections == []
    _assert_no_sensitive_text(captured.value)


def test_send_uses_fixed_post_headers_and_returns_bounded_json_response():
    body = b'{"choices":[{"message":{"content":"ok"}}]}'
    response = _FakeResponse(
        body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
            "X-Request-Id": "remote-request-canary",
        },
    )
    factory = _ConnectionFactory(response)
    transport = _transport(factory, max_response_bytes=64)
    request = _request(
        headers={
            "X-Deployment": "deployment-canary",
            "Host": "attacker.invalid",
            "Authorization": "Bearer attacker-credential",
            "Content-Type": "text/plain",
            "Accept": "text/plain",
            "Content-Length": "1",
            "Connection": "keep-alive",
        }
    )

    returned = transport.send(request, CancellationSource().token)

    connection = factory.connections[0]
    assert (connection.host, connection.port) == (
        "stepfun.example.test",
        443,
    )
    assert connection.connected is True
    assert connection.tunnel is None
    assert connection.request_line == (
        "POST",
        "/v1/chat/completions",
        True,
        True,
    )
    assert dict(connection.headers) == {
        "Host": "stepfun.example.test",
        "Authorization": f"Bearer {_CREDENTIAL}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Content-Length": str(len(request.body)),
        "Connection": "close",
        "X-Deployment": "deployment-canary",
    }
    assert len(connection.headers) == len(dict(connection.headers))
    assert connection.body == request.body
    assert returned.status_code == 200
    assert returned.content_type == "application/json; charset=utf-8"
    assert returned.body == body
    assert returned.remote_request_id == "remote-request-canary"
    assert response.read_sizes
    assert all(0 < size <= 65 for size in response.read_sizes)
    assert connection.sock.timeouts
    assert all(
        0 < timeout <= request.timeout_seconds for timeout in connection.sock.timeouts
    )
    assert response.closed is True
    assert connection.closed.is_set()
    _assert_no_sensitive_text(request)
    _assert_no_sensitive_text(transport)


def test_https_proxy_uses_connect_and_non_success_does_not_read_body():
    response = _FakeResponse(
        b'{"error":"remote-body-canary"}',
        status=401,
        headers={"Content-Type": "application/json"},
    )
    factory = _ConnectionFactory(response)
    transport = _transport(
        factory,
        proxies={
            "https": (f"http://proxy-user:{_PROXY_SECRET}@proxy.example.test:8080")
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
        "stepfun.example.test",
        443,
    )
    assert tunnel_headers["Host"] == "stepfun.example.test:443"
    assert tunnel_headers["Proxy-Authorization"].startswith("Basic ")
    assert "Proxy-Authorization" not in dict(connection.headers)
    assert returned.status_code == 401
    assert returned.body == b""
    assert response.read_calls == 0
    _assert_no_sensitive_text(returned)
    _assert_no_sensitive_text(transport)


def test_no_proxy_bypasses_configured_https_proxy():
    response_body = b'{"choices":[]}'
    response = _FakeResponse(
        response_body,
        headers={"Content-Length": str(len(response_body))},
    )
    factory = _ConnectionFactory(response)
    checked_hosts = []

    def bypass(host):
        checked_hosts.append(host)
        return host == "stepfun.example.test"

    transport = StdlibStepFunTransport(
        _ENDPOINT,
        _connection_factory=factory,
        _tls_context_factory=_FakeTlsContext,
        _proxy_getter=lambda: {"https": "http://proxy.example.test:8080"},
        _proxy_bypass=bypass,
    )

    transport.send(_request(), CancellationSource().token)

    connection = factory.connections[0]
    assert checked_hosts == ["stepfun.example.test"]
    assert (connection.host, connection.port) == (
        "stepfun.example.test",
        443,
    )
    assert connection.tunnel is None


@pytest.mark.parametrize(
    ("native_failure", "expected_kind"),
    [
        (
            socket.gaierror("dns-canary-must-not-leak"),
            StepFunTransportFailureKind.DNS_FAILED,
        ),
        (
            ssl.SSLError("tls-canary-must-not-leak"),
            StepFunTransportFailureKind.TLS_FAILED,
        ),
        (
            TimeoutError("connect-timeout-canary-must-not-leak"),
            StepFunTransportFailureKind.CONNECT_TIMEOUT,
        ),
        (
            OSError("connection-canary-must-not-leak"),
            StepFunTransportFailureKind.CONNECTION_FAILED,
        ),
    ],
)
def test_connect_failures_are_stably_mapped_without_native_text(
    native_failure,
    expected_kind,
):
    factory = _ConnectionFactory(
        _FakeResponse(b""),
        connect_failure=native_failure,
    )
    transport = _transport(factory)

    with pytest.raises(StepFunTransportFailure) as raised:
        transport.send(_request(), CancellationSource().token)

    assert raised.value.kind is expected_kind
    assert "canary-must-not-leak" not in (repr(raised.value) + str(raised.value))
    _assert_no_sensitive_text(raised.value)


@pytest.mark.parametrize(
    ("factory_kwargs", "expected_kind"),
    [
        (
            {"write_failure": TimeoutError("write-canary-must-not-leak")},
            StepFunTransportFailureKind.WRITE_TIMEOUT,
        ),
        (
            {"read_failure": TimeoutError("read-canary-must-not-leak")},
            StepFunTransportFailureKind.READ_TIMEOUT,
        ),
        (
            {
                "read_failure": http.client.BadStatusLine(
                    "protocol-canary-must-not-leak"
                )
            },
            StepFunTransportFailureKind.RESPONSE_PROTOCOL_INVALID,
        ),
    ],
)
def test_write_and_read_failures_are_stably_mapped(
    factory_kwargs,
    expected_kind,
):
    factory = _ConnectionFactory(_FakeResponse(b""), **factory_kwargs)
    transport = _transport(factory)

    with pytest.raises(StepFunTransportFailure) as raised:
        transport.send(_request(), CancellationSource().token)

    assert raised.value.kind is expected_kind
    assert "canary-must-not-leak" not in (repr(raised.value) + str(raised.value))
    _assert_no_sensitive_text(raised.value)


def test_oversized_truncated_and_encoded_responses_fail_stably():
    oversized = _FakeResponse(
        b"response-body-canary",
        headers={"Content-Length": "20"},
    )
    with pytest.raises(StepFunTransportFailure) as raised:
        _transport(
            _ConnectionFactory(oversized),
            max_response_bytes=10,
        ).send(_request(), CancellationSource().token)
    assert raised.value.kind is StepFunTransportFailureKind.RESPONSE_TOO_LARGE
    assert oversized.read_calls == 0
    _assert_no_sensitive_text(raised.value)

    truncated = _FakeResponse(
        b"partial-body-canary",
        headers={"Content-Length": "99"},
    )
    with pytest.raises(StepFunTransportFailure) as raised:
        _transport(_ConnectionFactory(truncated)).send(
            _request(),
            CancellationSource().token,
        )
    assert raised.value.kind is StepFunTransportFailureKind.RESPONSE_TRUNCATED
    assert "partial-body-canary" not in repr(raised.value)
    _assert_no_sensitive_text(raised.value)

    encoded = _FakeResponse(
        b"encoded-body-canary",
        headers={"Content-Encoding": "gzip"},
    )
    with pytest.raises(StepFunTransportFailure) as raised:
        _transport(_ConnectionFactory(encoded)).send(
            _request(),
            CancellationSource().token,
        )
    assert raised.value.kind is StepFunTransportFailureKind.RESPONSE_PROTOCOL_INVALID
    assert encoded.read_calls == 0
    _assert_no_sensitive_text(raised.value)


class _BlockingConnectConnection(_FakeConnection):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.connect_started = Event()

    def connect(self) -> None:
        self.connect_started.set()
        self.closed.wait(timeout=5)
        raise OSError("closed connect canary")


class _BlockingConnectFactory(_ConnectionFactory):
    connection_type = _BlockingConnectConnection


class _LateSuccessfulConnectConnection(_FakeConnection):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.connect_started = Event()
        self.release_connect = Event()
        self.late_socket_closed = Event()
        self.close_during_connect = Event()
        self.close_calls = 0
        self._connect_active = False

    def connect(self) -> None:
        self._connect_active = True
        self.connect_started.set()
        self.release_connect.wait(timeout=5)
        self.sock = _FakeSocket(self.late_socket_closed)
        self.connected = True
        self._connect_active = False

    def close(self) -> None:
        self.close_calls += 1
        if self._connect_active:
            self.close_during_connect.set()
        super().close()


class _LateSuccessfulConnectFactory(_ConnectionFactory):
    connection_type = _LateSuccessfulConnectConnection


class _BlockingReadConnection(_FakeConnection):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.read_started = Event()

    def getresponse(self):
        self.read_started.set()
        self.closed.wait(timeout=5)
        raise OSError("closed read canary")


class _BlockingReadFactory(_ConnectionFactory):
    connection_type = _BlockingReadConnection


class _BlockingBodyResponse(_FakeResponse):
    def __init__(self) -> None:
        super().__init__(b"")
        self.connection_interrupted: Event | None = None
        self.read_started = Event()
        self.read_finished = Event()
        self.closed_while_reading = Event()

    def read(self, size: int = -1) -> bytes:
        del size
        self.read_started.set()
        try:
            assert self.connection_interrupted is not None
            self.connection_interrupted.wait(timeout=5)
            raise OSError("closed body read canary")
        finally:
            self.read_finished.set()

    def close(self) -> None:
        if self.read_started.is_set() and not self.read_finished.is_set():
            self.closed_while_reading.set()
        super().close()


class _BlockingBodyConnection(_FakeConnection):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        assert isinstance(self.response, _BlockingBodyResponse)
        self.response.connection_interrupted = self.closed


class _BlockingBodyFactory(_ConnectionFactory):
    connection_type = _BlockingBodyConnection


@pytest.mark.parametrize(
    ("factory_type", "started_attribute"),
    [
        (_BlockingConnectFactory, "connect_started"),
        (_BlockingReadFactory, "read_started"),
    ],
)
def test_root_cancellation_closes_blocking_connect_or_read_without_leaks(
    factory_type,
    started_attribute,
):
    factory = factory_type(_FakeResponse(b""))
    transport = _transport(
        factory,
        proxies={
            "https": (f"http://proxy-user:{_PROXY_SECRET}@proxy.example.test:8080")
        },
    )
    cancellation = CancellationSource()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            transport.send,
            _request(),
            cancellation.token,
        )
        assert factory.created.wait(timeout=1)
        connection = factory.connections[0]
        assert getattr(connection, started_attribute).wait(timeout=1)
        cancellation.request(signal.SIGTERM)
        with pytest.raises(CancellationRequested) as raised:
            future.result(timeout=1)

    assert connection.closed.is_set()
    _assert_no_sensitive_text(raised.value)
    _assert_no_sensitive_text(transport)


def test_cancellation_interrupts_body_read_without_closing_response_concurrently():
    response = _BlockingBodyResponse()
    factory = _BlockingBodyFactory(response)
    transport = _transport(factory)
    cancellation = CancellationSource()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            transport.send,
            _request(),
            cancellation.token,
        )
        assert response.read_started.wait(timeout=1)
        cancellation.request(signal.SIGTERM)
        with pytest.raises(CancellationRequested):
            future.result(timeout=1)

    assert response.read_finished.is_set()
    assert not response.closed_while_reading.is_set()
    assert response.closed is True


def test_deadline_interrupts_body_read_without_closing_response_concurrently():
    response = _BlockingBodyResponse()
    factory = _BlockingBodyFactory(response)
    current_time = [0.0]
    transport = StdlibStepFunTransport(
        _ENDPOINT,
        _connection_factory=factory,
        _tls_context_factory=_FakeTlsContext,
        _proxy_getter=dict,
        _proxy_bypass=lambda _host: True,
        _clock=lambda: current_time[0],
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            transport.send,
            _request(),
            CancellationSource().token,
        )
        assert response.read_started.wait(timeout=1)
        current_time[0] = 4.0
        with pytest.raises(StepFunTransportFailure) as captured:
            future.result(timeout=1)

    assert captured.value.kind is StepFunTransportFailureKind.READ_TIMEOUT
    assert response.read_finished.is_set()
    assert not response.closed_while_reading.is_set()
    assert response.closed is True


def test_cancelled_late_connect_is_closed_by_worker_without_concurrent_close():
    factory = _LateSuccessfulConnectFactory(_FakeResponse(b""))
    transport = _transport(factory)
    cancellation = CancellationSource()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            transport.send,
            _request(),
            cancellation.token,
        )
        assert factory.created.wait(timeout=1)
        connection = factory.connections[0]
        assert isinstance(connection, _LateSuccessfulConnectConnection)
        assert connection.connect_started.wait(timeout=1)
        cancellation.request(signal.SIGTERM)
        with pytest.raises(CancellationRequested):
            future.result(timeout=1)

    assert not connection.close_during_connect.is_set()
    connection.release_connect.set()
    assert connection.late_socket_closed.wait(timeout=1)
    assert connection.close_calls == 1
