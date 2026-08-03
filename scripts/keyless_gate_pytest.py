"""为无密钥门禁生成不依赖第三方插件的精确 pytest 结果。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

_REPORT_OPTION = "--keyless-gate-report"
_RETRY_MARKS = frozenset({"flaky", "rerun", "reruns", "repeat"})
_state: dict[str, int] = {}


def _empty_state() -> dict[str, int]:
    return {
        "collected": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "deselected": 0,
        "skipped": 0,
        "xfail": 0,
        "xpass": 0,
        "retries": 0,
        "exit_code": 0,
    }


def pytest_addoption(parser) -> None:
    parser.addoption(
        _REPORT_OPTION,
        action="store",
        required=True,
        help="无密钥门禁机器结果路径",
    )


def pytest_configure(config) -> None:
    global _state
    _state = _empty_state()
    for mark in sorted(_RETRY_MARKS):
        config.addinivalue_line(
            "markers",
            f"{mark}: 无密钥门禁禁止的测试级自动重试标记",
        )
    expected_package_root = os.environ.get("KEYLESS_GATE_PACKAGE_ROOT")
    if expected_package_root:
        _verify_candidate_import(Path(expected_package_root))


def _verify_candidate_import(expected_root: Path) -> None:
    try:
        import video_auto_editor

        root = expected_root.resolve(strict=True)
        module_file = Path(video_auto_editor.__file__).resolve(strict=True)
        package_paths = tuple(
            Path(path).resolve(strict=True) for path in video_auto_editor.__path__
        )
    except (ImportError, OSError, TypeError):
        raise pytest.UsageError("无法从候选 wheel 导入 video_auto_editor") from None
    if not module_file.is_relative_to(root) or any(
        not path.is_relative_to(root) for path in package_paths
    ):
        raise pytest.UsageError("video_auto_editor 未从候选 wheel 安装根导入")


def pytest_collection_modifyitems(items) -> None:
    for item in items:
        if any(item.get_closest_marker(name) is not None for name in _RETRY_MARKS):
            _state["retries"] += 1


def pytest_collection_finish(session) -> None:
    _state["collected"] = len(session.items)


def pytest_deselected(items) -> None:
    _state["deselected"] += len(items)


def pytest_collectreport(report) -> None:
    if report.failed:
        _state["errors"] += 1
    elif report.skipped:
        _state["skipped"] += 1


def pytest_runtest_logreport(report) -> None:
    was_xfail = getattr(report, "wasxfail", None) is not None
    if report.outcome == "rerun":
        _state["retries"] += 1
        return
    if report.when != "call":
        if report.failed:
            _state["errors"] += 1
        elif report.skipped and not was_xfail:
            _state["skipped"] += 1
        return
    if was_xfail:
        _state["xfail" if report.skipped else "xpass"] += 1
    elif report.passed:
        _state["passed"] += 1
    elif report.failed:
        _state["failed"] += 1
    elif report.skipped:
        _state["skipped"] += 1


def pytest_sessionfinish(session, exitstatus) -> None:
    _state["exit_code"] = int(exitstatus)
    destination = Path(session.config.getoption(_REPORT_OPTION))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(_state, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)
