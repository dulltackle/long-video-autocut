"""Python 标准库实现的 StepFun Chat Completions HTTPS 传输。"""

from __future__ import annotations

import base64
import http.client
import re
import socket
import ssl
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import Enum
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit
from urllib.request import getproxies, proxy_bypass

from .interface import (
    ReadinessIssue,
    TextModelReadinessCode,
)
from .stepfun import (
    StepFunTransportFailure,
    StepFunTransportFailureKind,
    StepFunTransportRequest,
    StepFunTransportResponse,
)

_DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_RESPONSE_CHUNK_BYTES = 64 * 1024
_CANCELLATION_POLL_SECONDS = 0.01
_MAX_CONTENT_TYPE_LENGTH = 512
_MAX_REMOTE_REQUEST_ID_LENGTH = 256
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_PROTECTED_HEADERS = frozenset(
    {
        "accept",
        "authorization",
        "connection",
        "content-length",
        "content-type",
        "host",
        "proxy-authorization",
    }
)


class _CancellationView(Protocol):
    @property
    def cancelled(self) -> bool: ...

    @property
    def signal_number(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> bool: ...

    def raise_if_cancelled(self) -> None: ...


class _Connection(Protocol):
    sock: Any

    def set_tunnel(
        self,
        host: str,
        port: int,
        headers: Mapping[str, str] | None = None,
    ) -> None: ...

    def connect(self) -> None: ...

    def putrequest(
        self,
        method: str,
        url: str,
        *,
        skip_host: bool,
        skip_accept_encoding: bool,
    ) -> None: ...

    def putheader(self, header: str, *values: str) -> None: ...

    def endheaders(
        self,
        message_body: bytes | None = None,
        *,
        encode_chunked: bool = False,
    ) -> None: ...

    def getresponse(self) -> Any: ...

    def close(self) -> None: ...


class _OperationStage(str, Enum):
    CONNECT = "connect"
    WRITE = "write"
    READ = "read"


class _AbortReason(str, Enum):
    CANCELLATION = "cancellation"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class _Endpoint:
    host: str
    port: int
    selector: str
    host_header: str


@dataclass(frozen=True, slots=True, repr=False)
class _ProxyRoute:
    host: str
    port: int
    authorization: str | None = field(default=None, repr=False)


class _AbortController:
    """以一次状态发布终止当前连接，避免取消与超时竞态覆盖。"""

    __slots__ = (
        "_cleanup_lock",
        "_connect_finished",
        "_connection",
        "_connection_closed",
        "_lock",
        "_reason",
        "_response",
        "_stage",
        "_transport_socket",
    )

    def __init__(self, connection: _Connection) -> None:
        self._cleanup_lock = Lock()
        self._connect_finished = False
        self._connection = connection
        self._connection_closed = False
        self._lock = Lock()
        self._reason: _AbortReason | None = None
        self._response: Any = None
        self._stage = _OperationStage.CONNECT
        self._transport_socket: Any = None

    @property
    def reason(self) -> _AbortReason | None:
        with self._lock:
            return self._reason

    @property
    def stage(self) -> _OperationStage:
        with self._lock:
            return self._stage

    def enter(self, stage: _OperationStage) -> None:
        with self._lock:
            self._stage = stage

    def bind_response(self, response: Any) -> None:
        with self._lock:
            self._response = response
            already_aborted = self._reason is not None
        if already_aborted:
            _close_response(response)

    def bind_transport_socket(self) -> None:
        transport_socket = getattr(self._connection, "sock", None)
        with self._lock:
            self._transport_socket = transport_socket
            already_aborted = self._reason is not None
        if already_aborted:
            _interrupt_socket(transport_socket)

    def abort(self, reason: _AbortReason) -> bool:
        with self._lock:
            if self._reason is not None:
                return False
            self._reason = reason
            transport_socket = self._transport_socket
        if transport_socket is None:
            transport_socket = getattr(self._connection, "sock", None)
        # 只中断底层 I/O；HTTPResponse 由读取线程在退出后关闭，避免跨线程改写状态。
        _interrupt_socket(transport_socket)
        return True

    def finish_connect(self) -> None:
        with self._lock:
            self._connect_finished = True
            already_aborted = self._reason is not None
        if already_aborted:
            self._close_connection(abort=True)

    def close(self) -> None:
        with self._lock:
            response = self._response
            defer_connection_close = (
                self._reason is not None and not self._connect_finished
            )
        if response is not None:
            _close_response(response)
        if not defer_connection_close:
            self._close_connection(abort=False)

    def _close_connection(self, *, abort: bool) -> None:
        with self._cleanup_lock:
            with self._lock:
                if self._connection_closed:
                    return
                self._connection_closed = True
            if abort:
                _abort_connection(self._connection)
            else:
                _close_connection(self._connection)


class StdlibStepFunTransport:
    """具备系统 TLS、环境代理、有界响应与在途取消的生产传输。"""

    __slots__ = (
        "_clock",
        "_connection_factory",
        "_endpoint",
        "_max_response_bytes",
        "_proxy_bypass",
        "_proxy_getter",
        "_tls_context_factory",
    )

    def __init__(
        self,
        endpoint: str,
        *,
        _connection_factory: Callable[..., _Connection] = (http.client.HTTPSConnection),
        _tls_context_factory: Callable[[], Any] | None = None,
        _proxy_getter: Callable[[], Mapping[str, str]] = getproxies,
        _proxy_bypass: Callable[[str], bool] = proxy_bypass,
        _clock: Callable[[], float] = monotonic,
        _max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if not isinstance(endpoint, str):
            raise TypeError("StepFun HTTPS transport endpoint 必须是字符串")
        if (
            not isinstance(_max_response_bytes, int)
            or isinstance(_max_response_bytes, bool)
            or _max_response_bytes <= 0
        ):
            raise ValueError("StepFun 最大响应字节数必须是正整数")
        self._endpoint = endpoint
        self._connection_factory = _connection_factory
        self._tls_context_factory = _tls_context_factory or _system_tls_context
        self._proxy_getter = _proxy_getter
        self._proxy_bypass = _proxy_bypass
        self._clock = _clock
        self._max_response_bytes = _max_response_bytes

    def check_readiness(self) -> tuple[ReadinessIssue, ...]:
        """仅检查本地 endpoint、系统 CA 与代理配置。"""
        issues: list[ReadinessIssue] = []
        endpoint = _parse_endpoint(self._endpoint)
        if endpoint is None:
            issues.append(
                ReadinessIssue(
                    TextModelReadinessCode.HTTPS_REQUIRED,
                    {"field": "text_model_provider_config.endpoint"},
                )
            )

        try:
            context = self._tls_context_factory()
        except Exception:  # noqa: BLE001
            issues.append(_tls_issue("tls.ca_store_unavailable"))
        else:
            try:
                if not _tls_verification_enabled(context):
                    issues.append(_tls_issue("tls.verification_unavailable"))
                elif context.cert_store_stats().get("x509_ca", 0) < 1:
                    issues.append(_tls_issue("tls.ca_store_empty"))
            except Exception:  # noqa: BLE001
                issues.append(_tls_issue("tls.ca_store_unavailable"))

        if endpoint is not None:
            try:
                self._resolve_proxy(endpoint)
            except Exception:  # noqa: BLE001
                issues.append(
                    ReadinessIssue(
                        TextModelReadinessCode.TRANSPORT_CONFIGURATION_INVALID,
                        {
                            "field": "environment.https_proxy",
                            "reason_code": "value.invalid_format",
                        },
                    )
                )
        return tuple(issues)

    def send(
        self,
        request: StepFunTransportRequest,
        cancellation: _CancellationView,
    ) -> StepFunTransportResponse:
        """执行单次 POST；重试由上层 StepFun Adapter 统一拥有。"""
        if not isinstance(request, StepFunTransportRequest):
            raise TypeError("StepFun HTTPS transport 只接受传输请求")
        if not _is_cancellation_view(cancellation):
            raise TypeError("StepFun HTTPS transport 必须绑定取消令牌")
        cancellation.raise_if_cancelled()
        endpoint = _parse_endpoint(request.endpoint)
        if endpoint is None or request.endpoint != self._endpoint:
            raise StepFunTransportFailure(StepFunTransportFailureKind.CONNECTION_FAILED)
        if not _safe_header_value(request.credential):
            raise StepFunTransportFailure(StepFunTransportFailureKind.CONNECTION_FAILED)
        custom_headers = _safe_custom_headers(request.headers)
        if custom_headers is None:
            raise StepFunTransportFailure(StepFunTransportFailureKind.CONNECTION_FAILED)

        try:
            context = self._tls_context_factory()
            if not _tls_verification_enabled(context):
                raise StepFunTransportFailure(StepFunTransportFailureKind.TLS_FAILED)
            proxy = self._resolve_proxy(endpoint)
            connection = self._new_connection(
                endpoint,
                proxy,
                timeout_seconds=request.timeout_seconds,
                context=context,
            )
        except StepFunTransportFailure:
            raise
        except Exception as failure:  # noqa: BLE001
            raise _translate_native_failure(
                failure,
                _OperationStage.CONNECT,
            ) from None

        controller = _AbortController(connection)
        deadline = self._clock() + request.timeout_seconds
        try:
            self._connect(
                connection,
                controller,
                cancellation,
                deadline,
            )
            return self._exchange(
                connection,
                controller,
                endpoint,
                request,
                custom_headers,
                cancellation,
                deadline,
            )
        finally:
            controller.close()

    def _resolve_proxy(self, endpoint: _Endpoint) -> _ProxyRoute | None:
        if self._proxy_bypass(endpoint.host):
            return None
        proxies = self._proxy_getter()
        if not isinstance(proxies, Mapping):
            raise TypeError("代理发现必须返回映射")
        raw_proxy = proxies.get("https")
        if raw_proxy is None or raw_proxy == "":
            return None
        if not isinstance(raw_proxy, str):
            raise TypeError("HTTPS 代理必须是字符串")
        route = _parse_http_proxy(raw_proxy)
        if route is None:
            raise ValueError("HTTPS 代理格式不受支持")
        return route

    def _new_connection(
        self,
        endpoint: _Endpoint,
        proxy: _ProxyRoute | None,
        *,
        timeout_seconds: int,
        context: Any,
    ) -> _Connection:
        host = endpoint.host if proxy is None else proxy.host
        port = endpoint.port if proxy is None else proxy.port
        connection = self._connection_factory(
            host,
            port,
            timeout=float(timeout_seconds),
            context=context,
        )
        if proxy is not None:
            tunnel_host = (
                f"[{endpoint.host}]" if ":" in endpoint.host else endpoint.host
            )
            tunnel_headers = {"Host": f"{tunnel_host}:{endpoint.port}"}
            if proxy.authorization is not None:
                tunnel_headers["Proxy-Authorization"] = proxy.authorization
            connection.set_tunnel(
                endpoint.host,
                endpoint.port,
                headers=tunnel_headers,
            )
        return connection

    def _connect(
        self,
        connection: _Connection,
        controller: _AbortController,
        cancellation: _CancellationView,
        deadline: float,
    ) -> None:
        """把可能卡在 DNS 的连接建立隔离到不持有业务正文的 daemon。"""
        completed: Future[None] = Future()

        def connect() -> None:
            try:
                connection.connect()
            except Exception as failure:  # noqa: BLE001
                controller.finish_connect()
                completed.set_exception(failure)
            else:
                controller.finish_connect()
                completed.set_result(None)

        worker = Thread(
            target=connect,
            name="stepfun-https-connect",
            daemon=True,
        )
        worker.start()
        while not completed.done():
            if cancellation.wait(_CANCELLATION_POLL_SECONDS):
                controller.abort(_AbortReason.CANCELLATION)
                cancellation.raise_if_cancelled()
            if self._clock() >= deadline:
                controller.abort(_AbortReason.TIMEOUT)
                raise StepFunTransportFailure(
                    StepFunTransportFailureKind.CONNECT_TIMEOUT
                )
        try:
            completed.result()
        except Exception as failure:  # noqa: BLE001
            cancellation.raise_if_cancelled()
            raise _translate_native_failure(
                failure,
                _OperationStage.CONNECT,
            ) from None
        cancellation.raise_if_cancelled()
        if controller.reason is _AbortReason.TIMEOUT:
            raise StepFunTransportFailure(StepFunTransportFailureKind.CONNECT_TIMEOUT)

    def _exchange(
        self,
        connection: _Connection,
        controller: _AbortController,
        endpoint: _Endpoint,
        request: StepFunTransportRequest,
        custom_headers: tuple[tuple[str, str], ...],
        cancellation: _CancellationView,
        deadline: float,
    ) -> StepFunTransportResponse:
        controller.bind_transport_socket()
        done = Event()
        watcher = Thread(
            target=self._watch_cancellation_and_deadline,
            args=(controller, cancellation, deadline, done),
            name="stepfun-https-cancellation",
            daemon=True,
        )
        watcher.start()
        try:
            _set_socket_timeout(
                connection,
                max(0.001, deadline - self._clock()),
            )
            controller.enter(_OperationStage.WRITE)
            connection.putrequest(
                "POST",
                endpoint.selector,
                skip_host=True,
                skip_accept_encoding=True,
            )
            for name, value in _request_headers(
                endpoint,
                request,
                custom_headers,
            ):
                connection.putheader(name, value)
            connection.endheaders(request.body)
            cancellation.raise_if_cancelled()

            controller.enter(_OperationStage.READ)
            response = connection.getresponse()
            controller.bind_response(response)
            cancellation.raise_if_cancelled()
            result = _read_response(
                response,
                max_response_bytes=self._max_response_bytes,
            )
            cancellation.raise_if_cancelled()
            if controller.reason is _AbortReason.TIMEOUT:
                raise StepFunTransportFailure(StepFunTransportFailureKind.READ_TIMEOUT)
            return result
        except StepFunTransportFailure:
            raise
        except Exception as failure:  # noqa: BLE001
            translated = _failure_after_abort(
                failure,
                controller,
                cancellation,
            )
            raise translated from None
        finally:
            done.set()
            watcher.join()

    def _watch_cancellation_and_deadline(
        self,
        controller: _AbortController,
        cancellation: _CancellationView,
        deadline: float,
        done: Event,
    ) -> None:
        while not done.wait(_CANCELLATION_POLL_SECONDS):
            if cancellation.cancelled:
                controller.abort(_AbortReason.CANCELLATION)
                return
            if self._clock() >= deadline:
                controller.abort(_AbortReason.TIMEOUT)
                return


def _system_tls_context() -> ssl.SSLContext:
    """加载系统 CA，不采用会响应 SSLKEYLOGFILE 的便利构造器。"""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_default_certs(ssl.Purpose.SERVER_AUTH)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.set_alpn_protocols(["http/1.1"])
    return context


def _tls_verification_enabled(context: object) -> bool:
    candidate: Any = context
    try:
        return (
            candidate.check_hostname is True
            and candidate.verify_mode == ssl.CERT_REQUIRED
        )
    except Exception:  # noqa: BLE001
        return False


def _parse_endpoint(value: str) -> _Endpoint | None:
    if any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in value
    ):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        host = parsed.hostname.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return None
    selector = parsed.path or "/"
    if not selector.startswith("/") or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in selector
    ):
        return None
    target_port = 443 if port is None else port
    if not 1 <= target_port <= 65_535:
        return None
    bracketed_host = f"[{host}]" if ":" in host else host
    host_header = (
        bracketed_host if target_port == 443 else f"{bracketed_host}:{target_port}"
    )
    return _Endpoint(
        host=host,
        port=target_port,
        selector=selector,
        host_header=host_header,
    )


def _parse_http_proxy(value: str) -> _ProxyRoute | None:
    if any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in value
    ):
        return None
    candidate = value if "://" in value else f"http://{value}"
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme.casefold() != "http"
        or parsed.hostname is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        host = parsed.hostname.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return None
    target_port = 80 if port is None else port
    if not 1 <= target_port <= 65_535:
        return None
    authorization = None
    if parsed.username is not None:
        username = unquote(parsed.username)
        password = unquote(parsed.password or "")
        encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        authorization = f"Basic {encoded}"
    return _ProxyRoute(
        host=host,
        port=target_port,
        authorization=authorization,
    )


def _request_headers(
    endpoint: _Endpoint,
    request: StepFunTransportRequest,
    custom_headers: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    return (
        ("Host", endpoint.host_header),
        ("Authorization", f"Bearer {request.credential}"),
        ("Content-Type", "application/json"),
        ("Accept", "application/json"),
        ("Content-Length", str(len(request.body))),
        ("Connection", "close"),
        *custom_headers,
    )


def _safe_custom_headers(
    headers: object,
) -> tuple[tuple[str, str], ...] | None:
    if not isinstance(headers, Mapping):
        return None
    safe: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, value in headers.items():
        if (
            not isinstance(name, str)
            or _HEADER_NAME.fullmatch(name) is None
            or not isinstance(value, str)
            or not _safe_header_value(value)
        ):
            return None
        normalized = name.casefold()
        if normalized in _PROTECTED_HEADERS:
            continue
        if normalized in seen:
            return None
        seen.add(normalized)
        safe.append((name, value))
    return tuple(safe)


def _read_response(
    response: Any,
    *,
    max_response_bytes: int,
) -> StepFunTransportResponse:
    status = getattr(response, "status", None)
    if (
        not isinstance(status, int)
        or isinstance(status, bool)
        or not 100 <= status <= 599
    ):
        raise StepFunTransportFailure(
            StepFunTransportFailureKind.RESPONSE_PROTOCOL_INVALID
        )
    content_type = _bounded_header(
        response.getheader("Content-Type"),
        maximum=_MAX_CONTENT_TYPE_LENGTH,
    )
    remote_request_id = _remote_request_id(response)
    if not 200 <= status <= 299:
        return StepFunTransportResponse(
            status_code=status,
            content_type=content_type or "",
            body=b"",
            remote_request_id=remote_request_id,
        )

    raw_length = response.getheader("Content-Length")
    transfer_encoding = response.getheader("Transfer-Encoding")
    content_encoding = response.getheader("Content-Encoding")
    normalized_content_encoding = (
        content_encoding.strip().casefold()
        if isinstance(content_encoding, str)
        else content_encoding
    )
    normalized_transfer_encoding = (
        transfer_encoding.strip().casefold()
        if isinstance(transfer_encoding, str)
        else transfer_encoding
    )
    if normalized_content_encoding not in {None, "", "identity"}:
        raise StepFunTransportFailure(
            StepFunTransportFailureKind.RESPONSE_PROTOCOL_INVALID
        )
    if normalized_transfer_encoding not in {None, "", "chunked"}:
        raise StepFunTransportFailure(
            StepFunTransportFailureKind.RESPONSE_PROTOCOL_INVALID
        )
    if raw_length is not None and normalized_transfer_encoding not in {None, ""}:
        raise StepFunTransportFailure(
            StepFunTransportFailureKind.RESPONSE_PROTOCOL_INVALID
        )

    expected_length: int | None = None
    if raw_length is not None:
        if (
            not isinstance(raw_length, str)
            or not raw_length.isascii()
            or not raw_length.isdigit()
        ):
            raise StepFunTransportFailure(
                StepFunTransportFailureKind.RESPONSE_PROTOCOL_INVALID
            )
        expected_length = int(raw_length)
        if expected_length > max_response_bytes:
            raise StepFunTransportFailure(
                StepFunTransportFailureKind.RESPONSE_TOO_LARGE
            )

    body_parts: list[bytes] = []
    body_length = 0
    while True:
        remaining = max_response_bytes + 1 - body_length
        if remaining <= 0:
            raise StepFunTransportFailure(
                StepFunTransportFailureKind.RESPONSE_TOO_LARGE
            )
        chunk = response.read(min(_RESPONSE_CHUNK_BYTES, remaining))
        if not isinstance(chunk, bytes):
            raise StepFunTransportFailure(
                StepFunTransportFailureKind.RESPONSE_PROTOCOL_INVALID
            )
        if not chunk:
            break
        body_parts.append(chunk)
        body_length += len(chunk)
        if body_length > max_response_bytes:
            raise StepFunTransportFailure(
                StepFunTransportFailureKind.RESPONSE_TOO_LARGE
            )
    if expected_length is not None and body_length != expected_length:
        raise StepFunTransportFailure(StepFunTransportFailureKind.RESPONSE_TRUNCATED)
    return StepFunTransportResponse(
        status_code=status,
        content_type=content_type or "",
        body=b"".join(body_parts),
        remote_request_id=remote_request_id,
    )


def _remote_request_id(response: Any) -> str | None:
    for header in (
        "X-Request-Id",
        "X-Stepfun-Request-Id",
        "Request-Id",
    ):
        value = _bounded_header(
            response.getheader(header),
            maximum=_MAX_REMOTE_REQUEST_ID_LENGTH,
        )
        if value:
            return value
    return None


def _bounded_header(value: Any, *, maximum: int) -> str | None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or not value.isprintable()
        or "\r" in value
        or "\n" in value
    ):
        return None
    return value


def _safe_header_value(value: str) -> bool:
    if not value or not value.isprintable() or "\r" in value or "\n" in value:
        return False
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


def _failure_after_abort(
    failure: Exception,
    controller: _AbortController,
    cancellation: _CancellationView,
) -> StepFunTransportFailure:
    if controller.reason is _AbortReason.CANCELLATION or cancellation.cancelled:
        cancellation.raise_if_cancelled()
    if controller.reason is _AbortReason.TIMEOUT:
        return StepFunTransportFailure(_timeout_kind(controller.stage))
    return _translate_native_failure(failure, controller.stage)


def _translate_native_failure(
    failure: Exception,
    stage: _OperationStage,
) -> StepFunTransportFailure:
    if isinstance(failure, StepFunTransportFailure):
        return failure
    if isinstance(failure, http.client.IncompleteRead):
        kind = StepFunTransportFailureKind.RESPONSE_TRUNCATED
    elif isinstance(failure, http.client.RemoteDisconnected):
        kind = StepFunTransportFailureKind.CONNECTION_FAILED
    elif isinstance(failure, (ssl.CertificateError, ssl.SSLError)):
        kind = StepFunTransportFailureKind.TLS_FAILED
    elif isinstance(failure, socket.gaierror):
        kind = StepFunTransportFailureKind.DNS_FAILED
    elif isinstance(failure, (socket.timeout, TimeoutError)):
        kind = _timeout_kind(stage)
    elif isinstance(failure, http.client.HTTPException):
        kind = StepFunTransportFailureKind.RESPONSE_PROTOCOL_INVALID
    else:
        kind = StepFunTransportFailureKind.CONNECTION_FAILED
    return StepFunTransportFailure(kind)


def _timeout_kind(stage: _OperationStage) -> StepFunTransportFailureKind:
    if stage is _OperationStage.CONNECT:
        return StepFunTransportFailureKind.CONNECT_TIMEOUT
    if stage is _OperationStage.WRITE:
        return StepFunTransportFailureKind.WRITE_TIMEOUT
    return StepFunTransportFailureKind.READ_TIMEOUT


def _set_socket_timeout(connection: _Connection, timeout: float) -> None:
    sock = getattr(connection, "sock", None)
    if sock is not None:
        sock.settimeout(timeout)


def _abort_connection(connection: _Connection) -> None:
    sock = getattr(connection, "sock", None)
    _interrupt_socket(sock)
    _close_connection(connection)


def _interrupt_socket(sock: Any) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except (AttributeError, OSError):
        pass


def _close_connection(connection: _Connection) -> None:
    try:
        connection.close()
    except Exception:  # noqa: BLE001, S110
        pass


def _close_response(response: Any) -> None:
    try:
        response.close()
    except Exception:  # noqa: BLE001, S110
        pass


def _tls_issue(reason_code: str) -> ReadinessIssue:
    return ReadinessIssue(
        TextModelReadinessCode.TLS_VERIFICATION_UNAVAILABLE,
        {
            "component": "tls_ca",
            "operation": "tls.load_ca",
            "reason_code": reason_code,
        },
    )


def _is_cancellation_view(value: object) -> bool:
    return (
        hasattr(value, "cancelled")
        and hasattr(value, "signal_number")
        and callable(getattr(value, "wait", None))
        and callable(getattr(value, "raise_if_cancelled", None))
    )


__all__ = ["StdlibStepFunTransport"]
