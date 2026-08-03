"""为无法创建网络命名空间的测试进程限制非回环 socket。"""

from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path


class NetworkAccessDenied(OSError):
    """测试进程尝试访问非回环网络。"""


_INSTALLED = False
_ORIGINAL_CREATE_CONNECTION = socket.create_connection
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_ORIGINAL_GETHOSTBYNAME = socket.gethostbyname
_ORIGINAL_GETHOSTBYNAME_EX = socket.gethostbyname_ex
_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_CONNECT_EX = socket.socket.connect_ex
_ORIGINAL_BIND = socket.socket.bind
_ORIGINAL_SENDTO = socket.socket.sendto


def _audit_block() -> None:
    audit_path = os.environ.get("KEYLESS_GATE_NETWORK_AUDIT")
    if not audit_path:
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(Path(audit_path), flags, 0o600)
    try:
        os.write(descriptor, b"blocked\n")
    finally:
        os.close(descriptor)


def _deny() -> None:
    _audit_block()
    raise NetworkAccessDenied("无密钥门禁禁止非回环网络访问")


def _host_is_loopback(host: object) -> bool:
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str):
        return False
    normalized = host.strip().removesuffix(".").casefold()
    if normalized == "localhost":
        return True
    if "%" in normalized:
        normalized = normalized.split("%", 1)[0]
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _address_is_loopback(address: object) -> bool:
    if isinstance(address, (str, bytes)):
        return True  # AF_UNIX 地址不经过 IP 网络。
    return (
        isinstance(address, tuple) and bool(address) and _host_is_loopback(address[0])
    )


def _guard_address(address: object) -> None:
    if not _address_is_loopback(address):
        _deny()


def _guarded_create_connection(address, *args, **kwargs):
    _guard_address(address)
    return _ORIGINAL_CREATE_CONNECTION(address, *args, **kwargs)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if host is None:
        return _ORIGINAL_GETADDRINFO(host, *args, **kwargs)
    if not _host_is_loopback(host):
        _deny()
    results = _ORIGINAL_GETADDRINFO(host, *args, **kwargs)
    if any(not _address_is_loopback(result[4]) for result in results):
        _deny()
    return results


def _guarded_gethostbyname(host):
    if not _host_is_loopback(host):
        _deny()
    result = _ORIGINAL_GETHOSTBYNAME(host)
    if not _host_is_loopback(result):
        _deny()
    return result


def _guarded_gethostbyname_ex(host):
    if not _host_is_loopback(host):
        _deny()
    result = _ORIGINAL_GETHOSTBYNAME_EX(host)
    if any(not _host_is_loopback(address) for address in result[2]):
        _deny()
    return result


def _guarded_connect(self, address):
    _guard_address(address)
    return _ORIGINAL_CONNECT(self, address)


def _guarded_connect_ex(self, address):
    _guard_address(address)
    return _ORIGINAL_CONNECT_EX(self, address)


def _guarded_bind(self, address):
    _guard_address(address)
    return _ORIGINAL_BIND(self, address)


def _guarded_sendto(self, data, *args):
    if not args:
        raise TypeError("sendto 缺少目标地址")
    _guard_address(args[-1])
    return _ORIGINAL_SENDTO(self, data, *args)


def install() -> None:
    """幂等安装回环网络限制。"""

    global _INSTALLED
    if _INSTALLED:
        return
    socket.create_connection = _guarded_create_connection
    socket.getaddrinfo = _guarded_getaddrinfo
    socket.gethostbyname = _guarded_gethostbyname
    socket.gethostbyname_ex = _guarded_gethostbyname_ex
    # socket 的方法描述符不允许类型安全的直接赋值，需在进程级测试
    # sandbox 中动态替换。
    setattr(socket.socket, "connect", _guarded_connect)  # noqa: B010
    setattr(socket.socket, "connect_ex", _guarded_connect_ex)  # noqa: B010
    setattr(socket.socket, "bind", _guarded_bind)  # noqa: B010
    setattr(socket.socket, "sendto", _guarded_sendto)  # noqa: B010
    _INSTALLED = True


__all__ = ["NetworkAccessDenied", "install"]
