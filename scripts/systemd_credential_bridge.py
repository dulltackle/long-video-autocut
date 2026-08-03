#!/usr/bin/env python3
"""从 systemd credentials 向固定子进程临时注入 StepFun 凭据。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import resource
import socket
import stat
import struct
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


_CREDENTIAL_DIRECTORY_VARIABLE = "CREDENTIALS_DIRECTORY"
_CREDENTIAL_FILENAME = "stepfun_api_key"
_CREDENTIAL_VARIABLE = "STEPFUN_API_KEY"
_CREDENTIAL_DESCRIPTOR_VARIABLE = "RELEASE_GATE_SYSTEMD_CREDENTIAL_FD"
_HOST_NETWORK_NAMESPACE_VARIABLE = "RELEASE_GATE_SYSTEMD_HOST_NETNS"
_MAX_CREDENTIAL_BYTES = 4096
_MAX_PLAN_BYTES = 4 * 1024 * 1024
_CONFIGURATION_EXIT_CODE = 78
_PLAN_SCHEMA = "release_gate_plan.v1"
_TRUSTED_PLAN_ROOT = Path("/var/lib/video-auto-editor-release-plans")
_TRUSTED_PLAN_ANCESTOR_FLOOR = Path("/")
_PLAN_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_SYSTEM_CREDENTIAL_ROOT = Path("/run/credentials")
_SYSTEM_UNIT = re.compile(
    r"video-auto-editor-release-"
    r"(?P<slug>[a-z0-9][a-z0-9-]{0,63})-"
    r"(?P<phase>cold|rerun)\.service"
)
_RELEASE_TOOL_FILENAMES = frozenset(
    {
        "install-production.sh",
        "run_keyless_gate_network.sh",
        "run_release_gate.py",
        "systemd_credential_bridge.py",
        "validate_installed_delivery.py",
        "validate_release_evidence.py",
    }
)


class CredentialBridgeFailure(RuntimeError):
    """不携带路径、命令或凭据值的桥接失败。"""


def _regular_root_owned_file(path: Path) -> os.stat_result:
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise OSError
        metadata = path.lstat()
        parent = path.parent.lstat()
    except (OSError, RuntimeError):
        raise CredentialBridgeFailure("固定命令未绑定受信发布工具") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & 0o022
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != 0
        or parent.st_mode & 0o022
    ):
        raise CredentialBridgeFailure("固定命令未绑定受信发布工具")
    return metadata


def _sha256_file(path: Path) -> str:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise OSError
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(descriptor)
            if (
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                raise OSError
        finally:
            os.close(descriptor)
    except OSError:
        raise CredentialBridgeFailure("固定命令未绑定受信发布工具") from None
    return digest.hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _validate_plan_location(path: Path) -> None:
    try:
        trusted_root = _TRUSTED_PLAN_ROOT
        ancestor_floor = _TRUSTED_PLAN_ANCESTOR_FLOOR
        if (
            not trusted_root.is_absolute()
            or trusted_root.resolve(strict=True) != trusted_root
            or not ancestor_floor.is_absolute()
            or ancestor_floor.resolve(strict=True) != ancestor_floor
        ):
            raise OSError
        trusted_root.relative_to(ancestor_floor)
        relative = path.relative_to(trusted_root)
        if (
            len(relative.parts) != 2
            or _PLAN_SLUG.fullmatch(relative.parts[0]) is None
            or relative.parts[1] != "plan.json"
        ):
            raise OSError
        current = path.parent
        while True:
            metadata = current.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != 0
                or metadata.st_mode & 0o022
            ):
                raise OSError
            if current == ancestor_floor:
                break
            parent = current.parent
            if parent == current:
                raise OSError
            current = parent
    except (OSError, RuntimeError, ValueError):
        raise CredentialBridgeFailure("固定命令未绑定受信发布工具") from None


def _load_private_plan(path: Path, operator_gid: int) -> Mapping[str, Any]:
    try:
        if (
            path.name != "plan.json"
            or not path.is_absolute()
            or path.resolve(strict=True) != path
            or path.parent.resolve(strict=True) != path.parent
        ):
            raise OSError
        _validate_plan_location(path)
        parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != 0
            or parent.st_gid != operator_gid
            or stat.S_IMODE(parent.st_mode) != 0o710
        ):
            raise OSError
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != 0
                or before.st_gid != operator_gid
                or stat.S_IMODE(before.st_mode) != 0o440
                or before.st_size > _MAX_PLAN_BYTES
            ):
                raise OSError
            contents = bytearray()
            while len(contents) <= _MAX_PLAN_BYTES:
                chunk = os.read(
                    descriptor,
                    min(
                        1024 * 1024,
                        _MAX_PLAN_BYTES + 1 - len(contents),
                    ),
                )
                if not chunk:
                    break
                contents.extend(chunk)
            after = os.fstat(descriptor)
            if (
                len(contents) > _MAX_PLAN_BYTES
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                raise OSError
        finally:
            os.close(descriptor)
        plan = json.loads(
            bytes(contents).decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, RuntimeError, UnicodeError, ValueError, json.JSONDecodeError):
        raise CredentialBridgeFailure("固定命令未绑定受信发布工具") from None
    if not isinstance(plan, dict):
        raise CredentialBridgeFailure("固定命令未绑定受信发布工具")
    return plan


def _digest(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CredentialBridgeFailure("固定命令未绑定受信发布工具")
    return value


def _authorize_release_gate(
    command: tuple[str, ...], operator_gid: int
) -> None:
    if (
        len(command) != 6
        or command[1] != "-I"
        or command[3] not in {"execute", "rerun"}
        or command[4] != "--plan"
    ):
        raise CredentialBridgeFailure("固定命令未绑定受信发布工具")
    python = Path(command[0])
    gate = Path(command[2])
    plan_path = Path(command[5])
    try:
        expected_bridge = Path(__file__).resolve(strict=True)
    except (OSError, RuntimeError):
        raise CredentialBridgeFailure("固定命令未绑定受信发布工具") from None
    _regular_root_owned_file(gate)
    if gate.parent != expected_bridge.parent:
        raise CredentialBridgeFailure("固定命令未绑定受信发布工具")
    try:
        trusted_python = Path(sys.executable).resolve(strict=True)
    except (OSError, RuntimeError):
        raise CredentialBridgeFailure("固定命令未绑定受信发布工具") from None
    _regular_root_owned_file(python)
    if (
        python != trusted_python
        or not os.access(python, os.X_OK | os.R_OK)
        or gate.name != "run_release_gate.py"
        or not os.access(gate, os.R_OK)
    ):
        raise CredentialBridgeFailure("固定命令未绑定受信发布工具")
    plan = _load_private_plan(plan_path, operator_gid)
    try:
        release_tools = plan["automation"]["release_tools"]
        bridge_fact = plan["execution"]["credential_bridge"]
        source = plan["execution"]["credential_source"]
        credential_id = plan["execution"]["credential_id"]
        if (
            plan["schema_version"] != _PLAN_SCHEMA
            or not isinstance(release_tools, dict)
            or set(release_tools) != _RELEASE_TOOL_FILENAMES
            or not isinstance(bridge_fact, dict)
            or set(bridge_fact) != {"filename", "path", "sha256"}
            or source != "systemd_credentials"
            or credential_id != _CREDENTIAL_FILENAME
        ):
            raise KeyError
        bridge = Path(bridge_fact["path"])
        if (
            bridge_fact["filename"] != expected_bridge.name
            or bridge.resolve(strict=True) != expected_bridge
            or bridge != expected_bridge
            or _digest(bridge_fact["sha256"])
            != _digest(release_tools[expected_bridge.name])
            or _sha256_file(expected_bridge) != bridge_fact["sha256"]
            or _sha256_file(gate) != _digest(release_tools[gate.name])
        ):
            raise KeyError
    except (KeyError, TypeError, OSError, RuntimeError):
        raise CredentialBridgeFailure("固定命令未绑定受信发布工具") from None


def _operator_identity(value: str) -> int:
    if re.fullmatch(r"[1-9][0-9]{0,9}", value) is None:
        raise CredentialBridgeFailure("发布操作员身份不合法")
    identity = int(value)
    if identity > 2**31 - 1:
        raise CredentialBridgeFailure("发布操作员身份不合法")
    return identity


def _command_after_separator(
    arguments: Sequence[str],
) -> tuple[int, int, tuple[str, ...]]:
    if (
        len(arguments) < 6
        or arguments[0] != "--operator-uid"
        or arguments[2] != "--operator-gid"
    ):
        raise CredentialBridgeFailure("必须提供固定发布操作员身份")
    operator_uid = _operator_identity(arguments[1])
    operator_gid = _operator_identity(arguments[3])
    arguments = arguments[4:]
    if len(arguments) < 2 or arguments[0] != "--":
        raise CredentialBridgeFailure("必须在 -- 后提供固定命令")
    command = tuple(arguments[1:])
    if not os.path.isabs(command[0]):
        raise CredentialBridgeFailure("固定命令必须使用绝对路径")
    _authorize_release_gate(command, operator_gid)
    return operator_uid, operator_gid, command


def _initial_user_namespace(contents: str) -> bool:
    try:
        mappings = [
            tuple(int(value) for value in line.split())
            for line in contents.splitlines()
            if line.strip()
        ]
    except ValueError:
        return False
    return (
        len(mappings) == 1
        and mappings[0][0] == 0
        and mappings[0][1] == 0
        and mappings[0][2] >= 4_294_967_294
    )


def _systemd_unit_slug(directory: Path, action: str) -> str:
    match = _SYSTEM_UNIT.fullmatch(directory.name)
    expected_phase = "cold" if action == "execute" else "rerun"
    if match is None or match.group("phase") != expected_phase:
        raise CredentialBridgeFailure("systemd 系统服务上下文不可用")
    return match.group("slug")


def _trusted_systemd_credential_directory(
    environment: Mapping[str, str],
    action: str,
) -> Path:
    directory_value = environment.get(_CREDENTIAL_DIRECTORY_VARIABLE)
    try:
        uid_map = Path("/proc/self/uid_map").read_text(encoding="ascii")
        cgroup = Path("/proc/self/cgroup").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        raise CredentialBridgeFailure("systemd 系统服务上下文不可用") from None
    if (
        os.geteuid() != 0
        or not _initial_user_namespace(uid_map)
        or not directory_value
        or not os.path.isabs(directory_value)
    ):
        raise CredentialBridgeFailure("systemd 系统服务上下文不可用")
    directory = Path(directory_value)
    try:
        relative = directory.relative_to(_SYSTEM_CREDENTIAL_ROOT)
    except ValueError:
        raise CredentialBridgeFailure("systemd 系统服务上下文不可用") from None
    if len(relative.parts) != 1:
        raise CredentialBridgeFailure("systemd 系统服务上下文不可用")
    unit = relative.name
    _systemd_unit_slug(directory, action)
    if cgroup != f"0::/system.slice/{unit}\n":
        raise CredentialBridgeFailure("systemd 系统服务上下文不可用")
    return directory


def _verify_network_context(action: str) -> str:
    try:
        current = os.readlink("/proc/self/ns/net")
        host = os.readlink("/proc/1/ns/net")
        if action != "rerun":
            return host
        interfaces = {name for _index, name in socket.if_nameindex()}
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            request = struct.pack("16sH14x", b"lo", 0)
            response = fcntl.ioctl(probe.fileno(), 0x8913, request)
        _name, flags = struct.unpack("16sH14x", response)
    except (OSError, struct.error):
        raise CredentialBridgeFailure("缓存复跑网络隔离不可用") from None
    if current == host or interfaces != {"lo"} or not flags & 0x1:
        raise CredentialBridgeFailure("缓存复跑网络隔离不可用")
    return host


def _open_credential(directory: Path) -> int:
    directory_flags = (
        os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    file_flags = (
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    )

    try:
        directory_descriptor = os.open(directory, directory_flags)
    except OSError:
        raise CredentialBridgeFailure(
            "systemd credentials 目录不可用"
        ) from None
    try:
        directory_metadata = os.fstat(directory_descriptor)
        filesystem = os.fstatvfs(directory_descriptor)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or directory_metadata.st_uid != 0
            or directory_metadata.st_mode & 0o077
            or not filesystem.f_flag & os.ST_RDONLY
        ):
            raise CredentialBridgeFailure(
                "systemd credentials 目录不可用"
            )
        try:
            credential_descriptor = os.open(
                _CREDENTIAL_FILENAME,
                file_flags,
                dir_fd=directory_descriptor,
            )
        except OSError:
            raise CredentialBridgeFailure("StepFun 凭据不可用") from None
        try:
            metadata = os.fstat(credential_descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != 0
                or metadata.st_size == 0
                or metadata.st_size > _MAX_CREDENTIAL_BYTES
                or metadata.st_mode & 0o777 != 0o400
            ):
                raise CredentialBridgeFailure("StepFun 凭据不可用")
            os.set_inheritable(credential_descriptor, True)
            inherited_descriptor = credential_descriptor
            credential_descriptor = -1
        finally:
            if credential_descriptor >= 0:
                os.close(credential_descriptor)
    finally:
        os.close(directory_descriptor)
    return inherited_descriptor


def _drop_privileges(operator_uid: int, operator_gid: int) -> None:
    try:
        os.setgroups([])
        os.setgid(operator_gid)
        os.setuid(operator_uid)
    except OSError:
        raise CredentialBridgeFailure("无法降权到固定发布操作员") from None
    if (
        os.geteuid() != operator_uid
        or os.getegid() != operator_gid
        or os.getgroups()
    ):
        raise CredentialBridgeFailure("无法降权到固定发布操作员")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    try:
        environment = os.environ.copy()
        if _CREDENTIAL_VARIABLE in environment:
            raise CredentialBridgeFailure("拒绝预先存在的 StepFun 凭据")
        operator_uid, operator_gid, command = _command_after_separator(arguments)
        credential_directory = _trusted_systemd_credential_directory(
            environment,
            command[3],
        )
        if (
            Path(command[5]).parent.name
            != _systemd_unit_slug(credential_directory, command[3])
        ):
            raise CredentialBridgeFailure("固定命令未绑定受信发布工具")
        host_network_namespace = _verify_network_context(command[3])
        credential_descriptor = _open_credential(credential_directory)
        child_environment = {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        }
        child_environment[_CREDENTIAL_DESCRIPTOR_VARIABLE] = str(
            credential_descriptor
        )
        child_environment[_HOST_NETWORK_NAMESPACE_VARIABLE] = (
            host_network_namespace
        )
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        _drop_privileges(operator_uid, operator_gid)
        try:
            os.execve(command[0], command, child_environment)
        except OSError:
            os.close(credential_descriptor)
            raise CredentialBridgeFailure("无法启动固定命令") from None
    except CredentialBridgeFailure as failure:
        print(str(failure), file=sys.stderr)
        return _CONFIGURATION_EXIT_CODE
    except (OSError, ValueError):
        print("凭据桥接失败", file=sys.stderr)
        return _CONFIGURATION_EXIT_CODE
    raise AssertionError("execve 不应返回")


if __name__ == "__main__":
    raise SystemExit(main())
