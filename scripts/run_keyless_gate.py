#!/usr/bin/env python3
"""针对同一候选 wheel 执行完整无密钥测试门禁。"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import venv
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

if __package__ in {None, ""}:
    # 工作流用 ``python -I /trusted/.../run_keyless_gate.py`` 启动受信任裁判。
    # isolated mode 不会自动加入仓库根，因此只加入当前脚本自己的父仓库。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.validate_architecture import validate_wheel

_MANIFEST_SCHEMA = "keyless_gate_manifest.v1"
_EVIDENCE_SCHEMA = "keyless_gate_evidence.v1"
_LAYERS = (
    "unit_schema",
    "module_interfaces",
    "adapter_contracts",
    "fault_injection",
    "installation_contract",
)
_PRODUCTION_CREDENTIAL_VARIABLES = ("STEPFUN_API_KEY",)
_PYTEST_INJECTION_VARIABLES = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")
_COMMIT_BOUND_PATHS = (
    ".github/workflows/keyless-gate.yml",
    "build_backend.py",
    "pyproject.toml",
    "requirements-runtime.lock",
    "scripts",
    "tests",
    "video_auto_editor",
)


class GateConfigurationError(RuntimeError):
    """门禁输入或测试清单不满足封闭契约。"""


class GateOutcomeError(RuntimeError):
    """测试层结果不满足零异常结果契约。"""


@dataclass(frozen=True, slots=True)
class GateManifest:
    """每个测试模块恰好归属一个门禁层。"""

    layers: Mapping[str, tuple[str, ...]]


def load_gate_manifest(
    manifest_path: Path,
    *,
    source_root: Path,
) -> GateManifest:
    """读取并验证封闭测试分层清单。"""

    manifest_path = Path(manifest_path)
    source_root = Path(source_root).resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateConfigurationError("无法读取无密钥门禁清单") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "layers",
    }:
        raise GateConfigurationError("无密钥门禁清单顶层结构不合法")
    if payload["schema_version"] != _MANIFEST_SCHEMA:
        raise GateConfigurationError("无密钥门禁清单版本不受支持")
    raw_layers = payload["layers"]
    if not isinstance(raw_layers, dict) or tuple(raw_layers) != _LAYERS:
        raise GateConfigurationError("无密钥门禁层必须使用固定顺序与名称")

    normalized: dict[str, tuple[str, ...]] = {}
    assigned: list[str] = []
    for layer in _LAYERS:
        raw_paths = raw_layers[layer]
        if (
            not isinstance(raw_paths, list)
            or not raw_paths
            or any(not isinstance(item, str) for item in raw_paths)
        ):
            raise GateConfigurationError(f"门禁层 {layer} 必须包含非零测试模块")
        paths = tuple(raw_paths)
        if paths != tuple(sorted(paths)):
            raise GateConfigurationError(f"门禁层 {layer} 的测试模块必须排序")
        for relative_path in paths:
            path = Path(relative_path)
            if (
                path.is_absolute()
                or path.parts[:1] != ("tests",)
                or len(path.parts) < 2
                or any(part in {"", ".", ".."} for part in path.parts)
                or not path.name.startswith("test_")
                or path.suffix != ".py"
                or path.as_posix() != relative_path
            ):
                raise GateConfigurationError("门禁清单包含非法测试路径")
            if not (source_root / path).is_file():
                raise GateConfigurationError(f"门禁清单引用不存在测试：{relative_path}")
        normalized[layer] = paths
        assigned.extend(paths)

    duplicates = sorted(path for path in set(assigned) if assigned.count(path) != 1)
    if duplicates:
        raise GateConfigurationError("测试模块不能重复分配：" + ", ".join(duplicates))
    discovered = {
        path.relative_to(source_root).as_posix()
        for path in (source_root / "tests").rglob("test_*.py")
        if path.is_file() and "__pycache__" not in path.parts
    }
    assigned_set = set(assigned)
    unassigned = sorted(discovered - assigned_set)
    stale = sorted(assigned_set - discovered)
    if unassigned:
        raise GateConfigurationError("发现未分配测试模块：" + ", ".join(unassigned))
    if stale:
        raise GateConfigurationError("门禁清单包含陈旧测试模块：" + ", ".join(stale))
    return GateManifest(MappingProxyType(normalized))


def validate_gate_outcome(layer: str, outcome: Mapping[str, object]) -> None:
    """拒绝空层、失败、错误、筛除、跳过、预期失败、意外通过和重跑。"""

    required = {
        "collected",
        "passed",
        "failed",
        "errors",
        "deselected",
        "skipped",
        "xfail",
        "xpass",
        "retries",
        "exit_code",
    }
    if not isinstance(outcome, Mapping) or set(outcome) != required:
        raise GateOutcomeError(f"{layer}: 门禁结果字段不完整")
    normalized: dict[str, int] = {}
    for field in required:
        value = outcome[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise GateOutcomeError(f"{layer}: {field} 必须是非负整数")
        normalized[field] = value
    if normalized["collected"] == 0:
        raise GateOutcomeError(f"{layer}: collected 必须非零")
    for field in (
        "failed",
        "errors",
        "deselected",
        "skipped",
        "xfail",
        "xpass",
        "retries",
    ):
        if normalized[field] != 0:
            raise GateOutcomeError(f"{layer}: {field} 必须为零")
    if normalized["exit_code"] != 0:
        raise GateOutcomeError(f"{layer}: exit_code 必须为零")
    if normalized["passed"] != normalized["collected"]:
        raise GateOutcomeError(f"{layer}: passed 必须等于 collected")


def validate_wheel_source(wheel_path: Path, *, source_root: Path) -> str:
    """验证候选发布结构、源码字节一致性并返回 SHA-256。"""

    wheel_path = Path(wheel_path)
    source_root = Path(source_root).resolve()
    try:
        if not wheel_path.is_file():
            raise FileNotFoundError(wheel_path)
        validate_wheel(wheel_path, source_root=source_root)
        with wheel_path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except Exception:  # noqa: BLE001 - 任一构建物校验异常都必须失败关闭
        raise GateConfigurationError(
            "候选 wheel 不存在、结构非法或与源码不一致"
        ) from None


def _run_checked(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            tuple(command),
            cwd=cwd,
            env=None if env is None else dict(env),
            check=True,
            capture_output=capture_output,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        raise GateConfigurationError("无密钥门禁外部步骤执行失败") from None


def _credential_free_environment() -> dict[str, str]:
    """复制进程环境，并移除凭据及可注入 pytest 行为的变量。"""

    environment = os.environ.copy()
    for variable in (
        *_PRODUCTION_CREDENTIAL_VARIABLES,
        *_PYTEST_INJECTION_VARIABLES,
    ):
        environment.pop(variable, None)
    return environment


def _validate_commit(source_root: Path, expected: str) -> str:
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", expected):
        raise GateConfigurationError("候选 commit SHA 格式不合法")
    completed = _run_checked(
        ("git", "rev-parse", "HEAD"),
        cwd=source_root,
        env=_credential_free_environment(),
        capture_output=True,
    )
    actual = completed.stdout.strip()
    if actual != expected:
        raise GateConfigurationError("候选 commit SHA 与源码检出不一致")
    try:
        tracked = subprocess.run(
            (
                "git",
                "diff",
                "--no-ext-diff",
                "--quiet",
                "HEAD",
                "--",
                *_COMMIT_BOUND_PATHS,
            ),
            cwd=source_root,
            env=_credential_free_environment(),
            check=False,
        )
    except OSError:
        raise GateConfigurationError("无法验证候选工作树") from None
    if tracked.returncode == 1:
        raise GateConfigurationError("候选工作树包含未提交的相关改动")
    if tracked.returncode != 0:
        raise GateConfigurationError("无法验证候选工作树")
    untracked = _run_checked(
        (
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            *_COMMIT_BOUND_PATHS,
        ),
        cwd=source_root,
        env=_credential_free_environment(),
        capture_output=True,
    )
    if untracked.stdout:
        raise GateConfigurationError("候选工作树包含未提交的相关文件")
    return actual


def _create_candidate_python(working: Path) -> Path:
    environment = working / "candidate-venv"
    try:
        venv.EnvBuilder(
            with_pip=True,
            system_site_packages=True,
            clear=True,
        ).create(environment)
    except OSError:
        raise GateConfigurationError("无法创建候选 wheel 测试环境") from None
    test_python = environment / "bin" / "python"
    pytest_spec = importlib.util.find_spec("pytest")
    if pytest_spec is None or pytest_spec.origin is None:
        raise GateConfigurationError("门禁宿主没有可复用的 pytest")
    pytest_site = Path(pytest_spec.origin).resolve().parents[1]
    completed = _run_checked(
        (
            str(test_python),
            "-I",
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ),
        cwd=working,
        env=_credential_free_environment(),
        capture_output=True,
    )
    try:
        purelib = Path(completed.stdout.strip()).resolve(strict=True)
        (purelib / "keyless-gate-host-tools.pth").write_text(
            f"{pytest_site}\n",
            encoding="utf-8",
        )
    except OSError:
        raise GateConfigurationError("无法向候选环境暴露门禁测试工具") from None
    return test_python


def _install_candidate(
    test_python: Path,
    wheel_path: Path,
    *,
    source_root: Path,
) -> None:
    _run_checked(
        (
            str(test_python),
            "-I",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--no-deps",
            "--force-reinstall",
            str(wheel_path),
        ),
        cwd=source_root,
        env=_credential_free_environment(),
    )
    _run_checked(
        (str(test_python), "-I", "-c", "import pytest"),
        cwd=source_root,
        env=_credential_free_environment(),
    )


def _candidate_package_root(
    test_python: Path,
    *,
    source_root: Path,
) -> Path:
    probe = (
        "import json, sysconfig, video_auto_editor\n"
        "print(json.dumps({"
        "'file': video_auto_editor.__file__, "
        "'paths': list(video_auto_editor.__path__), "
        "'purelib': sysconfig.get_path('purelib')"
        "}))\n"
    )
    completed = _run_checked(
        (str(test_python), "-I", "-c", probe),
        cwd=source_root,
        env=_credential_free_environment(),
        capture_output=True,
    )
    try:
        payload = json.loads(completed.stdout)
        module_file = Path(payload["file"]).resolve(strict=True)
        package_paths = tuple(
            Path(path).resolve(strict=True) for path in payload["paths"]
        )
        purelib = Path(payload["purelib"]).resolve(strict=True)
    except (OSError, TypeError, KeyError, json.JSONDecodeError):
        raise GateConfigurationError("无法确认候选 wheel 的安装导入位置") from None
    if not package_paths:
        raise GateConfigurationError("候选 wheel 没有封闭包路径")
    package_root = module_file.parent
    if package_root.parent != purelib or package_root.name != "video_auto_editor":
        raise GateConfigurationError("候选 wheel 未从测试环境自己的 purelib 导入")
    if any(
        path != package_root and not path.is_relative_to(package_root)
        for path in package_paths
    ):
        raise GateConfigurationError("候选 wheel 暴露了安装根以外的包路径")
    if module_file.is_relative_to(source_root):
        raise GateConfigurationError("测试导入回退到了源码包")
    return package_root


def _create_import_support(working: Path, source_root: Path) -> Path:
    """只暴露测试辅助代码，不把源码包加入测试解释器路径。"""

    support = working / "import-support"
    support.mkdir()
    for name in ("tests", "scripts"):
        os.symlink(
            source_root / name,
            support / name,
            target_is_directory=True,
        )
    os.symlink(source_root / "build_backend.py", support / "build_backend.py")
    return support


def _python_path(harness_root: Path, import_support: Path) -> str:
    """优先加载受信任 guard/plugin，再暴露候选测试辅助代码。"""

    return os.pathsep.join((str(harness_root / "scripts"), str(import_support)))


def _validate_network_namespace(
    *,
    parent_netns: str,
    current_netns: str,
    interfaces: Sequence[str],
) -> None:
    """确认 wrapper 确实进入了一个只含回环接口的新网络命名空间。"""

    if not parent_netns or not current_netns:
        raise GateConfigurationError("缺少网络命名空间身份")
    if current_netns == parent_netns:
        raise GateConfigurationError("仍处于父网络命名空间")
    if tuple(sorted(interfaces)) != ("lo",):
        raise GateConfigurationError("网络命名空间必须仅包含 lo 接口")


def _probe_network(harness_root: Path, working: Path) -> dict[str, object]:
    mode = os.environ.get("KEYLESS_GATE_NETWORK_MODE", "python_guard")
    if mode not in {"network_namespace", "python_guard"}:
        raise GateConfigurationError("未知的无密钥门禁网络隔离模式")
    if mode == "network_namespace":
        try:
            current_netns = os.readlink("/proc/self/ns/net")
            interfaces = tuple(name for _, name in socket.if_nameindex())
        except OSError:
            raise GateConfigurationError("无法验证网络命名空间") from None
        _validate_network_namespace(
            parent_netns=os.environ.get("KEYLESS_GATE_PARENT_NETNS", ""),
            current_netns=current_netns,
            interfaces=interfaces,
        )

    audit = working / "network-probe.audit"
    program = """
import socket

listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen(1)
client = socket.create_connection(listener.getsockname(), timeout=1)
server, _ = listener.accept()
client.close()
server.close()
listener.close()
try:
    socket.create_connection(("198.51.100.7", 443), timeout=0.01)
except OSError:
    pass
else:
    raise SystemExit(91)
"""
    environment = _credential_free_environment()
    environment.update(
        {
            "KEYLESS_GATE_NETWORK_AUDIT": str(audit),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(harness_root / "scripts"),
        }
    )
    _run_checked(
        (sys.executable, "-c", program),
        cwd=working,
        env=environment,
    )
    try:
        blocked = audit.read_text(encoding="utf-8").splitlines()
    except OSError:
        blocked = []
    if blocked != ["blocked"]:
        raise GateConfigurationError("回环网络 guard 未阻断外部连接探针")
    return {
        "external_blocked": True,
        "loopback_allowed": True,
        "mode": mode,
    }


def _invalid_outcome(exit_code: int = 1) -> dict[str, int]:
    return {
        "collected": 0,
        "passed": 0,
        "failed": 0,
        "errors": 1,
        "deselected": 0,
        "skipped": 0,
        "xfail": 0,
        "xpass": 0,
        "retries": 0,
        "exit_code": max(1, exit_code),
    }


def _read_layer_outcome(
    report: Path,
    *,
    process_exit_code: int,
) -> dict[str, int]:
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
        outcome = {key: value for key, value in payload.items()}
    except (OSError, json.JSONDecodeError):
        return _invalid_outcome(process_exit_code)
    if outcome.get("exit_code") != process_exit_code:
        return _invalid_outcome(process_exit_code)
    return outcome


def _run_layer(
    layer: str,
    test_modules: tuple[str, ...],
    *,
    test_python: Path,
    package_root: Path,
    source_root: Path,
    harness_root: Path,
    import_support: Path,
    working: Path,
) -> dict[str, int]:
    layer_directory = working / "layers" / layer
    layer_directory.mkdir(parents=True)
    report = layer_directory / "pytest.json"
    audit = layer_directory / "network.audit"
    environment = _credential_free_environment()
    environment.update(
        {
            "KEYLESS_GATE_NETWORK_AUDIT": str(audit),
            "KEYLESS_GATE_PACKAGE_ROOT": str(package_root),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": _python_path(harness_root, import_support),
            "PYTHONSAFEPATH": "1",
        }
    )
    command = (
        str(test_python),
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-p",
        "keyless_gate_pytest",
        "--strict-config",
        "--strict-markers",
        "--import-mode=importlib",
        "-c",
        str(source_root / "pyproject.toml"),
        "-o",
        f"pythonpath={import_support}",
        "--keyless-gate-report",
        str(report),
        *(str(source_root / module) for module in test_modules),
    )
    try:
        completed = subprocess.run(
            command,
            cwd=source_root,
            env=environment,
            check=False,
        )
    except OSError:
        return _invalid_outcome()
    outcome = _read_layer_outcome(
        report,
        process_exit_code=completed.returncode,
    )
    try:
        blocked_attempts = audit.read_text(encoding="utf-8").splitlines()
    except OSError:
        blocked_attempts = []
    if blocked_attempts:
        outcome = dict(outcome)
        outcome["errors"] = int(outcome.get("errors", 0)) + len(blocked_attempts)
    return outcome


def _write_evidence(destination: Path, evidence: Mapping[str, Any]) -> None:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)


def run_gate(
    *,
    wheel_path: Path,
    commit_sha: str,
    evidence_path: Path,
    source_root: Path,
    manifest_path: Path,
    harness_root: Path | None = None,
    test_python_override: Path | None = None,
) -> bool:
    source_root = source_root.resolve()
    harness_root = (
        Path(__file__).resolve().parents[1]
        if harness_root is None
        else harness_root.resolve()
    )
    evidence: dict[str, Any] = {
        "schema_version": _EVIDENCE_SCHEMA,
        "candidate": {
            "commit_sha": commit_sha,
            "wheel_filename": Path(wheel_path).name,
            "wheel_sha256": "",
        },
        "layers": {},
        "credential_mode": "absent",
        "network": {
            "external_blocked": False,
            "loopback_allowed": False,
            "mode": os.environ.get(
                "KEYLESS_GATE_NETWORK_MODE",
                "python_guard",
            ),
        },
        "success": False,
    }
    try:
        _validate_commit(source_root, commit_sha)
        digest = validate_wheel_source(wheel_path, source_root=source_root)
        evidence["candidate"]["wheel_sha256"] = digest
        manifest = load_gate_manifest(
            manifest_path,
            source_root=source_root,
        )
        with tempfile.TemporaryDirectory(prefix="keyless-gate-") as raw_working:
            working = Path(raw_working)
            evidence["network"] = _probe_network(harness_root, working)
            test_python = (
                Path(test_python_override).resolve()
                if test_python_override is not None
                else _create_candidate_python(working)
            )
            _install_candidate(
                test_python,
                Path(wheel_path).resolve(),
                source_root=source_root,
            )
            package_root = _candidate_package_root(
                test_python,
                source_root=source_root,
            )
            import_support = _create_import_support(working, source_root)
            all_layers_passed = True
            for layer, test_modules in manifest.layers.items():
                outcome = _run_layer(
                    layer,
                    test_modules,
                    test_python=test_python,
                    package_root=package_root,
                    source_root=source_root,
                    harness_root=harness_root,
                    import_support=import_support,
                    working=working,
                )
                evidence["layers"][layer] = outcome
                try:
                    validate_gate_outcome(layer, outcome)
                except GateOutcomeError:
                    all_layers_passed = False
            evidence["success"] = all_layers_passed
    except GateConfigurationError:
        evidence["success"] = False
    finally:
        _write_evidence(evidence_path, evidence)
    return bool(evidence["success"])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="对同一候选 wheel 执行完整无密钥门禁",
    )
    project_root = Path(__file__).resolve().parents[1]
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--source-root", type=Path, default=project_root)
    parser.add_argument(
        "--harness-root",
        type=Path,
        default=project_root,
        help="受信任门禁脚本所在仓库；可与候选源码仓库分离",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=project_root / "tests" / "keyless_gate_manifest.json",
    )
    parser.add_argument(
        "--test-python",
        type=Path,
        help="使用预配测试 Python；默认创建隔离候选 venv",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    succeeded = run_gate(
        wheel_path=arguments.wheel,
        commit_sha=arguments.commit_sha,
        evidence_path=arguments.evidence,
        source_root=arguments.source_root,
        manifest_path=arguments.manifest,
        harness_root=arguments.harness_root,
        test_python_override=arguments.test_python,
    )
    if not succeeded:
        print("无密钥门禁未通过；详见机器可读证据", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
