import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from scripts.run_keyless_gate import (
    GateConfigurationError,
    GateOutcomeError,
    _candidate_package_root,
    _install_candidate,
    _python_path,
    _validate_commit,
    _validate_network_namespace,
    load_gate_manifest,
    validate_gate_outcome,
    validate_wheel_source,
)
from scripts.validate_architecture import APPROVED_PACKAGE_FILES

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GATE_MANIFEST = PROJECT_ROOT / "tests" / "keyless_gate_manifest.json"
NETWORK_ENTRYPOINT = PROJECT_ROOT / "scripts" / "run_keyless_gate_network.sh"
NETWORK_GUARD = PROJECT_ROOT / "scripts" / "keyless_gate_network_guard.py"
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "keyless-gate.yml"

EXPECTED_LAYERS = {
    "unit_schema",
    "module_interfaces",
    "adapter_contracts",
    "fault_injection",
    "installation_contract",
}


def _environment_without(*variables):
    environment = os.environ.copy()
    for variable in variables:
        environment.pop(variable, None)
    return environment


def test_gate_manifest_assigns_every_test_module_once_and_keeps_each_layer_nonempty():
    manifest = load_gate_manifest(GATE_MANIFEST, source_root=PROJECT_ROOT)
    assigned = [path for paths in manifest.layers.values() for path in paths]
    discovered = {
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in (PROJECT_ROOT / "tests").rglob("test_*.py")
        if "__pycache__" not in path.parts
    }

    assert set(manifest.layers) == EXPECTED_LAYERS
    assert all(manifest.layers[layer] for layer in EXPECTED_LAYERS)
    assert len(assigned) == len(set(assigned))
    assert set(assigned) == discovered


def test_gate_manifest_rejects_a_future_unassigned_test_module(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    known = {
        "unit_schema": "tests/test_unit.py",
        "module_interfaces": "tests/test_module.py",
        "adapter_contracts": "tests/test_adapter.py",
        "fault_injection": "tests/test_fault.py",
        "installation_contract": "tests/test_installation.py",
    }
    for relative_path in known.values():
        (tmp_path / relative_path).write_text("", encoding="utf-8")
    nested = tests / "e2e"
    nested.mkdir()
    (nested / "test_future.py").write_text("", encoding="utf-8")
    manifest_path = tests / "keyless_gate_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "keyless_gate_manifest.v1",
                "layers": {
                    layer: [relative_path] for layer, relative_path in known.items()
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(GateConfigurationError, match="未分配"):
        load_gate_manifest(manifest_path, source_root=tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("deselected", 1),
        ("skipped", 1),
        ("xfail", 1),
        ("xpass", 1),
        ("retries", 1),
        ("failed", 1),
        ("errors", 1),
    ],
)
def test_gate_outcome_rejects_nonpassing_or_retried_results(field, value):
    outcome = {
        "collected": 3,
        "passed": 3,
        "failed": 0,
        "errors": 0,
        "deselected": 0,
        "skipped": 0,
        "xfail": 0,
        "xpass": 0,
        "retries": 0,
        "exit_code": 0,
    }
    outcome[field] = value

    with pytest.raises(GateOutcomeError, match=field):
        validate_gate_outcome("unit_schema", outcome)


def test_pytest_gate_plugin_reports_skip_xfail_xpass_and_retry_marks(tmp_path):
    test_module = tmp_path / "test_outcomes.py"
    test_module.write_text(
        """
import pytest

def test_passes():
    pass

@pytest.mark.skip(reason="gate must reject")
def test_skips():
    pass

@pytest.mark.xfail(reason="gate must reject")
def test_xfails():
    assert False

@pytest.mark.xfail(reason="gate must reject")
def test_xpasses():
    pass

@pytest.mark.flaky
def test_retry_marker_is_forbidden():
    pass
""",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"
    module_skip = tmp_path / "test_module_skip.py"
    module_skip.write_text(
        "import pytest\npytest.skip('gate must reject', allow_module_level=True)\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "scripts.keyless_gate_pytest",
            "--keyless-gate-report",
            str(report),
            str(test_module),
            str(module_skip),
        ],
        cwd=tmp_path,
        env={
            **_environment_without("KEYLESS_GATE_PACKAGE_ROOT"),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONPATH": str(PROJECT_ROOT),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(report.read_text(encoding="utf-8")) == {
        "collected": 5,
        "passed": 2,
        "failed": 0,
        "errors": 0,
        "deselected": 0,
        "skipped": 2,
        "xfail": 1,
        "xpass": 1,
        "retries": 1,
        "exit_code": 0,
    }


def test_pytest_gate_plugin_reports_deselected_items(tmp_path):
    test_module = tmp_path / "test_selection.py"
    test_module.write_text(
        "def test_keep(): pass\ndef test_drop(): pass\n",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "scripts.keyless_gate_pytest",
            "--keyless-gate-report",
            str(report),
            "-k",
            "keep",
            str(test_module),
        ],
        cwd=tmp_path,
        env={
            **_environment_without("KEYLESS_GATE_PACKAGE_ROOT"),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONPATH": str(PROJECT_ROOT),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(report.read_text(encoding="utf-8")) == {
        "collected": 1,
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "deselected": 1,
        "skipped": 0,
        "xfail": 0,
        "xpass": 0,
        "retries": 0,
        "exit_code": 0,
    }


def test_pytest_gate_plugin_rejects_package_imported_outside_candidate_root(tmp_path):
    test_module = tmp_path / "test_import_origin.py"
    test_module.write_text("def test_placeholder(): pass\n", encoding="utf-8")
    report = tmp_path / "report.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "scripts.keyless_gate_pytest",
            "--keyless-gate-report",
            str(report),
            str(test_module),
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "KEYLESS_GATE_PACKAGE_ROOT": str(tmp_path / "candidate-site"),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONPATH": str(PROJECT_ROOT),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "候选 wheel" in (completed.stdout + completed.stderr)


def _write_candidate_wheel(
    tmp_path,
    *,
    source_root=PROJECT_ROOT,
    stale_source=False,
):
    wheel = tmp_path / "video_auto_editor-4.7.0-py3-none-any.whl"
    dist_info = "video_auto_editor-4.7.0.dist-info"
    with ZipFile(wheel, "w", compression=ZIP_DEFLATED) as archive:
        for relative_path in APPROVED_PACKAGE_FILES:
            contents = (source_root / relative_path).read_bytes()
            if stale_source and relative_path == "video_auto_editor/__init__.py":
                contents += b"\nSTALE = True\n"
            archive.writestr(relative_path, contents)
        archive.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.4\n"
            "Name: video-auto-editor\n"
            "Version: 4.7.0\n"
            "Requires-Python: <3.13,>=3.12.3\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\n"
            "Generator: keyless-gate-test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
        archive.writestr(
            f"{dist_info}/entry_points.txt",
            "[console_scripts]\nvideo-auto-editor = video_auto_editor.cli:main\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")
    return wheel


def _write_isolated_source_snapshot(tmp_path):
    archive_path = tmp_path / "source.tar"
    source_root = tmp_path / "source"
    source_root.mkdir()
    subprocess.run(
        [
            "git",
            "archive",
            "--format=tar",
            f"--output={archive_path}",
            "HEAD",
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )
    with tarfile.open(archive_path) as archive:
        archive.extractall(source_root, filter="data")

    shutil.rmtree(source_root / "tests")
    tests = source_root / "tests"
    tests.mkdir()
    layer_paths = {
        "unit_schema": "tests/test_unit.py",
        "module_interfaces": "tests/test_module.py",
        "adapter_contracts": "tests/test_adapter.py",
        "fault_injection": "tests/test_fault.py",
        "installation_contract": "tests/test_installation.py",
    }
    for relative_path in layer_paths.values():
        (source_root / relative_path).write_text(
            "def test_candidate_import():\n"
            "    import video_auto_editor\n"
            "    assert video_auto_editor.__file__\n",
            encoding="utf-8",
        )
    manifest_path = tests / "keyless_gate_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "keyless_gate_manifest.v1",
                "layers": {
                    layer: [relative_path]
                    for layer, relative_path in layer_paths.items()
                },
            }
        ),
        encoding="utf-8",
    )
    for script_name in (
        "keyless_gate_network_guard.py",
        "keyless_gate_pytest.py",
        "sitecustomize.py",
    ):
        shutil.copy2(
            PROJECT_ROOT / "scripts" / script_name,
            source_root / "scripts" / script_name,
        )
    subprocess.run(["git", "init", "-q"], cwd=source_root, check=True)
    subprocess.run(["git", "add", "--all"], cwd=source_root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Gate Test",
            "-c",
            "user.email=gate@example.invalid",
            "commit",
            "-qm",
            "test: create source snapshot",
        ],
        cwd=source_root,
        check=True,
    )
    commit_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return source_root, manifest_path, commit_sha


def test_gate_rejects_tracked_and_untracked_relevant_worktree_changes(tmp_path):
    source_root = tmp_path / "source"
    package = source_root / "video_auto_editor"
    tests = source_root / "tests"
    package.mkdir(parents=True)
    tests.mkdir()
    module = package / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=source_root, check=True)
    subprocess.run(["git", "add", "--all"], cwd=source_root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Gate Test",
            "-c",
            "user.email=gate@example.invalid",
            "commit",
            "-qm",
            "test: create clean candidate",
        ],
        cwd=source_root,
        check=True,
    )
    commit_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    module.write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(GateConfigurationError, match="工作树"):
        _validate_commit(source_root, commit_sha)

    module.write_text("VALUE = 1\n", encoding="utf-8")
    (tests / "test_untracked.py").write_text(
        "def test_untracked(): pass\n",
        encoding="utf-8",
    )
    with pytest.raises(GateConfigurationError, match="工作树"):
        _validate_commit(source_root, commit_sha)


def test_gate_accepts_a_wheel_identical_to_source_and_rejects_stale_bytes(tmp_path):
    matching = _write_candidate_wheel(tmp_path)
    stale_root = tmp_path / "stale"
    stale_root.mkdir()
    stale = _write_candidate_wheel(stale_root, stale_source=True)

    matching_digest = validate_wheel_source(matching, source_root=PROJECT_ROOT)
    assert len(matching_digest) == 64
    assert matching_digest == matching_digest.lower()

    with pytest.raises(GateConfigurationError, match="候选 wheel"):
        validate_wheel_source(stale, source_root=PROJECT_ROOT)


def test_candidate_install_force_reinstalls_the_exact_wheel(tmp_path):
    wheel = tmp_path / "candidate.whl"
    wheel.write_bytes(b"wheel-canary")
    calls = tmp_path / "calls.jsonl"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        f"""#!{sys.executable}
import json
import sys
from pathlib import Path

with Path({str(calls)!r}).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    _install_candidate(fake_python, wheel, source_root=tmp_path)

    invocations = [
        json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()
    ]
    assert "--force-reinstall" in invocations[0]
    assert str(wheel) in invocations[0]


def test_candidate_import_rejects_a_global_package_outside_test_python_purelib(
    tmp_path,
):
    global_package = tmp_path / "global-site" / "video_auto_editor"
    global_package.mkdir(parents=True)
    (global_package / "__init__.py").write_text("", encoding="utf-8")
    candidate_purelib = tmp_path / "candidate-venv" / "site-packages"
    candidate_purelib.mkdir(parents=True)
    source_root = tmp_path / "source"
    source_root.mkdir()
    fake_python = tmp_path / "python"
    fake_python.write_text(
        f"""#!{sys.executable}
import json

print(json.dumps({{
    "file": {str(global_package / "__init__.py")!r},
    "paths": [{str(global_package)!r}],
    "purelib": {str(candidate_purelib)!r},
}}))
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    with pytest.raises(GateConfigurationError, match="测试环境"):
        _candidate_package_root(fake_python, source_root=source_root)


def test_gate_runner_executes_every_layer_and_writes_candidate_bound_evidence(tmp_path):
    source_root, manifest_path, commit_sha = _write_isolated_source_snapshot(tmp_path)
    wheel = _write_candidate_wheel(tmp_path, source_root=source_root)
    installed_package = tmp_path / "candidate-site" / "video_auto_editor"
    installed_package.mkdir(parents=True)
    (installed_package / "__init__.py").write_text("", encoding="utf-8")
    fake_python = tmp_path / "gate-python"
    fake_python.write_text(
        f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if "--keyless-gate-report" in args:
    if any(variable in os.environ for variable in (
        "STEPFUN_API_KEY",
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
    )):
        raise SystemExit(73)
    report = Path(args[args.index("--keyless-gate-report") + 1])
    report.write_text(json.dumps({{
        "collected": 1,
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "deselected": 0,
        "skipped": 0,
        "xfail": 0,
        "xpass": 0,
        "retries": 0,
        "exit_code": 0,
    }}), encoding="utf-8")
elif "-c" in args:
    print(json.dumps({{
        "file": {str(installed_package / "__init__.py")!r},
        "paths": [{str(installed_package)!r}],
        "purelib": {str(installed_package.parent)!r},
    }}))
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    evidence = tmp_path / "evidence.json"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.run_keyless_gate",
            "--wheel",
            str(wheel),
            "--commit-sha",
            commit_sha,
            "--evidence",
            str(evidence),
            "--source-root",
            str(source_root),
            "--manifest",
            str(manifest_path),
            "--test-python",
            str(fake_python),
        ],
        cwd=PROJECT_ROOT,
        env={
            **os.environ,
            "KEYLESS_GATE_NETWORK_MODE": "python_guard",
            "PYTHONDONTWRITEBYTECODE": "1",
            "STEPFUN_API_KEY": "production-secret-must-be-removed",
            "PYTEST_ADDOPTS": "-k nothing",
            "PYTEST_PLUGINS": "module_that_must_not_load",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "keyless_gate_evidence.v1"
    assert payload["candidate"] == {
        "commit_sha": commit_sha,
        "wheel_filename": wheel.name,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
    }
    assert set(payload["layers"]) == EXPECTED_LAYERS
    assert all(result["collected"] == 1 for result in payload["layers"].values())
    assert payload["network"] == {
        "external_blocked": True,
        "loopback_allowed": True,
        "mode": "python_guard",
    }
    assert payload["credential_mode"] == "absent"
    assert payload["release_tools"] == {
        name: hashlib.sha256(
            (PROJECT_ROOT / "scripts" / name).read_bytes()
        ).hexdigest()
        for name in (
            "install-production.sh",
            "run_keyless_gate_network.sh",
            "run_release_gate.py",
            "systemd_credential_bridge.py",
            "validate_installed_delivery.py",
            "validate_release_evidence.py",
        )
    }
    assert payload["success"] is True


def test_gate_runner_uses_real_pytest_from_an_isolated_candidate_env(tmp_path):
    source_root, manifest_path, commit_sha = _write_isolated_source_snapshot(tmp_path)
    wheel = _write_candidate_wheel(tmp_path, source_root=source_root)
    evidence = tmp_path / "evidence.json"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.run_keyless_gate",
            "--wheel",
            str(wheel),
            "--commit-sha",
            commit_sha,
            "--evidence",
            str(evidence),
            "--source-root",
            str(source_root),
            "--manifest",
            str(manifest_path),
        ],
        cwd=PROJECT_ROOT,
        env={
            **os.environ,
            "KEYLESS_GATE_NETWORK_MODE": "python_guard",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert all(
        result["passed"] == result["collected"] == 1
        for result in payload["layers"].values()
    )
    assert payload["success"] is True


def test_python_network_guard_allows_loopback_and_records_external_attempt(tmp_path):
    audit = tmp_path / "network-audit.log"
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
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        env={
            **os.environ,
            "KEYLESS_GATE_NETWORK_AUDIT": str(audit),
            "PYTHONPATH": os.pathsep.join(
                [str(NETWORK_GUARD.parent), os.environ.get("PYTHONPATH", "")]
            ),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert audit.read_text(encoding="utf-8").splitlines() == ["blocked"]


def test_namespace_attestation_rejects_the_parent_network_namespace():
    with pytest.raises(GateConfigurationError, match="父网络命名空间"):
        _validate_network_namespace(
            parent_netns="net:[42]",
            current_netns="net:[42]",
            interfaces=("lo",),
        )


def test_namespace_attestation_rejects_any_non_loopback_interface():
    with pytest.raises(GateConfigurationError, match="仅包含 lo"):
        _validate_network_namespace(
            parent_netns="net:[41]",
            current_netns="net:[42]",
            interfaces=("eth0", "lo"),
        )


def test_namespace_attestation_accepts_a_distinct_loopback_only_namespace():
    _validate_network_namespace(
        parent_netns="net:[41]",
        current_netns="net:[42]",
        interfaces=("lo",),
    )


def test_network_entrypoint_has_a_deterministic_python_guard_fallback(tmp_path):
    completed = subprocess.run(
        [
            str(NETWORK_ENTRYPOINT),
            sys.executable,
            "-c",
            "import os; print(os.environ['KEYLESS_GATE_NETWORK_MODE'])",
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "KEYLESS_GATE_FORCE_PYTHON_GUARD": "1",
            "KEYLESS_GATE_REQUIRE_NAMESPACE": "0",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "python_guard"


def test_network_entrypoint_never_falls_back_when_namespace_is_required(tmp_path):
    completed = subprocess.run(
        [str(NETWORK_ENTRYPOINT), sys.executable, "-c", "pass"],
        cwd=tmp_path,
        env={
            **os.environ,
            "KEYLESS_GATE_FORCE_PYTHON_GUARD": "1",
            "KEYLESS_GATE_REQUIRE_NAMESPACE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "网络命名空间" in completed.stderr


def test_network_entrypoint_scrubs_spoofed_context_in_forced_guard(tmp_path):
    observation = tmp_path / "environment.json"
    fake_command = tmp_path / "gate-command"
    fake_command.write_text(
        f"""#!{sys.executable}
import json
import os
from pathlib import Path

Path({str(observation)!r}).write_text(json.dumps({{
    "legacy_uid": os.environ.get("KEYLESS_GATE_ORIGINAL_UID"),
    "legacy_gid": os.environ.get("KEYLESS_GATE_ORIGINAL_GID"),
    "preset_mode": os.environ.get("KEYLESS_GATE_NETWORK_MODE"),
    "attestation_fd": os.environ.get("RELEASE_GATE_NETWORK_ATTESTATION_FD"),
    "host_netns": os.environ.get("RELEASE_GATE_SYSTEMD_HOST_NETNS"),
}}), encoding="utf-8")
raise SystemExit(90)
""",
        encoding="utf-8",
    )
    fake_command.chmod(0o755)
    completed = subprocess.run(
        [str(NETWORK_ENTRYPOINT), str(fake_command), "argument"],
        cwd=tmp_path,
        env={
            **os.environ,
            "KEYLESS_GATE_FORCE_PYTHON_GUARD": "1",
            "KEYLESS_GATE_NETWORK_MODE": "network_namespace",
            "KEYLESS_GATE_ORIGINAL_UID": "0",
            "KEYLESS_GATE_ORIGINAL_GID": "0",
            "RELEASE_GATE_NETWORK_ATTESTATION_FD": "9",
            "RELEASE_GATE_SYSTEMD_HOST_NETNS": "net:[123]",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 90
    assert completed.stderr == ""
    assert json.loads(observation.read_text(encoding="utf-8")) == {
        "legacy_uid": None,
        "legacy_gid": None,
        "preset_mode": "python_guard",
        "attestation_fd": None,
        "host_netns": None,
    }


def test_network_entrypoint_refuses_attestation_outside_private_namespace(
    tmp_path,
):
    sudo_log = tmp_path / "sudo.json"
    fake_sudo = tmp_path / "sudo"
    fake_sudo.write_text(
        f"#!{sys.executable}\n"
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"Path({str(sudo_log)!r}).write_text("
        "json.dumps(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    fake_sudo.chmod(0o755)
    fake_command = tmp_path / "gate-command"
    fake_command.write_text(f"#!{sys.executable}\nraise SystemExit(90)\n")
    fake_command.chmod(0o755)
    read_descriptor, write_descriptor = os.pipe()
    try:
        completed = subprocess.run(
            [
                str(NETWORK_ENTRYPOINT),
                str(fake_command),
                "argument",
            ],
            cwd=tmp_path,
            env={
                **_environment_without("KEYLESS_GATE_FORCE_PYTHON_GUARD"),
                "KEYLESS_GATE_REQUIRE_NAMESPACE": "1",
                "RELEASE_GATE_NETWORK_ATTESTATION_FD": str(write_descriptor),
                "RELEASE_GATE_SYSTEMD_HOST_NETNS": os.readlink(
                    "/proc/self/ns/net"
                ),
                "PATH": os.pathsep.join([str(tmp_path), os.environ["PATH"]]),
            },
            pass_fds=(write_descriptor,),
            check=False,
            capture_output=True,
            text=True,
        )
        os.close(write_descriptor)
        write_descriptor = -1
        attestation_payload = os.read(read_descriptor, 4096)
    finally:
        if write_descriptor >= 0:
            os.close(write_descriptor)
        os.close(read_descriptor)

    assert completed.returncode == 1
    assert not sudo_log.exists()
    assert attestation_payload == b""
    assert "不在 systemd 仅回环网络命名空间" in completed.stderr


@pytest.mark.parametrize("attestation_fd", ["01", "02"])
def test_network_entrypoint_rejects_noncanonical_attestation_fd(
    tmp_path,
    attestation_fd,
):
    fake_command = tmp_path / "gate-command"
    fake_command.write_text(f"#!{sys.executable}\nraise SystemExit(90)\n")
    fake_command.chmod(0o755)

    completed = subprocess.run(
        [str(NETWORK_ENTRYPOINT), str(fake_command)],
        cwd=tmp_path,
        env={
            **_environment_without("KEYLESS_GATE_FORCE_PYTHON_GUARD"),
            "KEYLESS_GATE_REQUIRE_NAMESPACE": "1",
            "RELEASE_GATE_NETWORK_ATTESTATION_FD": attestation_fd,
            "PATH": os.pathsep.join([str(tmp_path), os.environ["PATH"]]),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "证明文件描述符不合法" in completed.stderr


def test_network_entrypoint_drops_reacquirable_privileges():
    entrypoint = NETWORK_ENTRYPOINT.read_text(encoding="utf-8")

    assert entrypoint.count(
        "LC_ALL=C /usr/bin/grep -qxE '[3-9]|[1-9][0-9]+'"
    ) == 2
    assert "*[!0-9]*|0*|1|2)" not in entrypoint
    assert entrypoint.count("2>/dev/null || true)") == 2
    assert '/usr/bin/grep -qxE "net:\\[[0-9]+\\]"' in entrypoint
    assert 'sudo_command=/usr/bin/sudo' in entrypoint
    assert "-n -E" not in entrypoint
    assert "拒绝跨 sudo 保留调用者环境" in entrypoint
    assert 'host_netns=${RELEASE_GATE_SYSTEMD_HOST_NETNS:-}' in entrypoint
    assert entrypoint.index(
        'attestation_fd=${RELEASE_GATE_NETWORK_ATTESTATION_FD:-}',
        entrypoint.index('parent_netns=$("$readlink_command" /proc/self/ns/net)'),
    ) < entrypoint.index('exec "$sudo_command" -n')
    assert "--nnp" in entrypoint
    assert "--inh-caps=-all" in entrypoint
    assert "--ambient-caps=-all" in entrypoint
    assert "--bounding-set=-all" in entrypoint
    assert "original_uid=${KEYLESS_GATE_ORIGINAL_UID" not in entrypoint
    assert "original_gid=${KEYLESS_GATE_ORIGINAL_GID" not in entrypoint
    assert 'if [ "$target_uid" -eq 0 ]' in entrypoint
    assert entrypoint.index('if [ "$caller_uid" -eq 0 ]') < entrypoint.index(
        "KEYLESS_GATE_FORCE_PYTHON_GUARD"
    )


def test_network_entrypoint_does_not_change_uid_with_a_rootless_namespace():
    entrypoint = NETWORK_ENTRYPOINT.read_text(encoding="utf-8")

    assert "--map-root-user" not in entrypoint


def test_gate_python_path_loads_trusted_guards_before_candidate_support(tmp_path):
    trusted_root = tmp_path / "trusted"
    import_support = tmp_path / "candidate-support"

    assert _python_path(trusted_root, import_support).split(os.pathsep) == [
        str(trusted_root / "scripts"),
        str(import_support),
    ]


def test_gate_runner_can_start_isolated_from_a_hostile_candidate_directory(tmp_path):
    canary = tmp_path / "candidate-sitecustomize-ran"
    (tmp_path / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(canary)!r}).touch()\n",
        encoding="utf-8",
    )
    hostile_scripts = tmp_path / "scripts"
    hostile_scripts.mkdir()
    (hostile_scripts / "validate_architecture.py").write_text(
        "raise SystemExit(97)\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(PROJECT_ROOT / "scripts/run_keyless_gate.py"),
            "--help",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert not canary.exists()


def test_workflow_runs_every_required_event_and_builds_the_candidate_once():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "pull_request:" in workflow
    assert "pull_request_target:" not in workflow
    assert "push:" in workflow
    assert "branches: [main]" in workflow
    assert "workflow_dispatch:" in workflow
    assert "paths:" not in workflow
    assert "concurrency:" in workflow
    assert "timeout-minutes: 60" in workflow
    assert workflow.count(
        "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    ) == 1
    assert workflow.count(
        "uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"
    ) == 1
    assert workflow.count(
        "uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
    ) == 2
    assert workflow.count("persist-credentials: false") == 1
    assert 'trusted_root="/opt/keyless-gate"' in workflow
    assert 'candidate_root="$GITHUB_WORKSPACE"' in workflow
    assert 'sudo install -d -o root -g root -m 0755 "$trusted_root/scripts"' in workflow
    assert 'sudo chmod -R a-w "$trusted_root"' in workflow
    validation_loop_start = workflow.index("for source in")
    validation_loop = workflow[
        validation_loop_start : workflow.index("done", validation_loop_start)
    ]
    assert "requirements-build.lock" in validation_loop
    assert "requirements-runtime.lock" in validation_loop
    assert workflow.count("-m build --wheel") == 1
    assert "--no-isolation" in workflow
    assert workflow.count('--wheel "$CANDIDATE_WHEEL"') == 3
    assert "scripts/installed_acceptance_composition.py" in workflow
    assert "scripts/run_installed_acceptance.py" in workflow
    assert "scripts/run_release_gate.py" in workflow
    assert "scripts/systemd_credential_bridge.py" in workflow
    assert "scripts/validate_release_evidence.py" in workflow
    assert "scripts/validate_installed_delivery.py" in workflow
    assert "scripts/install-production.sh" in workflow
    assert workflow.count('APT_SNAPSHOT_ID="20260725T000000Z"') == 3
    assert workflow.count('sudo "$trusted_root/scripts/install-production.sh"') == 1
    assert '--runtime-lock "$runtime_lock"' in workflow
    assert '--wheelhouse "$wheelhouse"' in workflow
    assert '--installation-prefix "$installation_prefix"' in workflow
    assert "python -m video_auto_editor" not in workflow
    assert workflow.count('"$trusted_root/scripts/run_keyless_gate_network.sh"') == 3
    assert workflow.count('KEYLESS_GATE_REQUIRE_NAMESPACE: "1"') == 2
    assert '"$python_path" -I "$trusted_root/scripts/run_keyless_gate.py"' in workflow
    assert '--harness-root "$trusted_root"' in workflow
    assert '--harness-root "$trusted_root/scripts"' in workflow
    assert workflow.count('--source-root "$candidate_root"') == 2
    build_at = workflow.index("-m build --wheel")
    gate_at = workflow.index(
        '"$python_path" -I "$trusted_root/scripts/run_keyless_gate.py"'
    )
    install_at = workflow.index(
        'sudo "$trusted_root/scripts/install-production.sh"'
    )
    acceptance_at = workflow.index(
        '"$python_path" -I "$trusted_root/scripts/run_installed_acceptance.py"'
    )
    assert build_at < gate_at < install_at < acceptance_at
    assert "installed-acceptance-evidence.json" in workflow
    assert "evidence/installation-manifest.json" in workflow
    assert "evidence/READY" in workflow
    assert "${{ runner.temp }}/production-installation-manifest.json" not in workflow
    assert "if-no-files-found: error" in workflow


def test_workflow_uploads_one_deterministic_release_bundle_with_unix_modes():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    bundle_at = workflow.index("- name: 固化保留 Unix 权限的确定性发布包")
    upload_at = workflow.index("- name: 保存候选与机器可读证据")
    failure_record_at = workflow.index("- name: 记录 CI 失败现场")
    bundle_step = workflow[bundle_at:upload_at]
    upload_step = workflow[upload_at:failure_record_at]

    assert bundle_at < upload_at
    assert "mapfile -t wheels" in bundle_step
    assert "-name '*.whl' -print" in bundle_step
    assert "if [[ ${#wheels[@]} -ne 1 ]]" in bundle_step
    assert 'wheel_filename=$(basename -- "${wheels[0]}")' in bundle_step
    assert "candidate/*.whl" not in upload_step

    expected_members = {
        '"candidate/$wheel_filename"',
        "candidate/commit-sha",
        "candidate/requirements-build.lock",
        "candidate/requirements-runtime.lock",
        "evidence/READY",
        "evidence/installation-manifest.json",
        "evidence/installed-acceptance-evidence.json",
        "evidence/keyless-gate-evidence.json",
        "scripts/install-production.sh",
        "scripts/run_keyless_gate_network.sh",
        "scripts/run_release_gate.py",
        "scripts/systemd_credential_bridge.py",
        "scripts/validate_installed_delivery.py",
        "scripts/validate_release_evidence.py",
    }
    members_match = re.search(
        r"bundle_members=\(\n(?P<members>.*?)\n\s+\)",
        bundle_step,
        re.DOTALL,
    )
    assert members_match is not None
    actual_members = {
        line.strip()
        for line in members_match.group("members").splitlines()
        if line.strip()
    }
    assert actual_members == expected_members
    assert "LC_ALL=C sort -z" in bundle_step

    assert bundle_step.count("sudo install -o root -g root -m 0444") == 2
    assert 'expected_mode=444' in bundle_step
    assert (
        "scripts/install-production.sh|scripts/run_keyless_gate_network.sh"
        in bundle_step
    )
    assert "expected_mode=555" in bundle_step
    assert "stat -c '%a:%u:%g'" in bundle_step
    assert '!= "$expected_mode:0:0"' in bundle_step

    checksum_at = bundle_step.index("sha256sum --")
    tar_at = bundle_step.index("sudo tar")
    assert checksum_at < tar_at
    assert "sha256sum -- \"$@\" > BUNDLE-SHA256SUMS" in bundle_step
    assert 'chmod 0444 "$trusted_root/BUNDLE-SHA256SUMS"' in bundle_step
    assert "--sort=name" in bundle_step
    assert "--mtime='@0'" in bundle_step
    assert "--clamp-mtime" in bundle_step
    assert "--owner=0" in bundle_step
    assert "--group=0" in bundle_step
    assert "--numeric-owner" in bundle_step
    assert "BUNDLE-SHA256SUMS" in bundle_step
    assert '"${bundle_members[@]}"' in bundle_step
    assert 'chmod 0444 "$release_bundle"' in bundle_step

    assert "path: ${{ runner.temp }}/release-bundle.tar" in upload_step
    assert "archive: false" in upload_step
    assert "if-no-files-found: error" in upload_step
    assert "if: always()" not in upload_step
    assert "path: |" not in upload_step


def test_workflow_persists_failed_ci_evidence_as_nonrelease_diagnostics():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    record_at = workflow.index("- name: 记录 CI 失败现场")
    upload_at = workflow.index("- name: 保存 CI 失败诊断")
    record_step = workflow[record_at:upload_at]
    upload_step = workflow[upload_at:]

    assert record_at < upload_at
    assert "if: failure()" in record_step
    assert '"schema_version": "keyless-gate-failure.v1"' in record_step
    assert '"classification": "ci_gate_failure_non_retryable"' in record_step
    assert '"release_eligible": False' in record_step
    assert "${{ github.sha }}" in record_step
    assert "${{ github.run_id }}" in record_step
    assert "${{ github.run_attempt }}" in record_step
    assert "if: failure()" in upload_step
    assert "keyless-gate-failure-${{ github.run_id }}-${{ github.run_attempt }}" in upload_step
    assert "path: |" in upload_step
    assert "${{ runner.temp }}/keyless-gate-failure.json" in upload_step
    assert "${{ runner.temp }}/keyless-candidate/*.whl" in upload_step
    assert "${{ runner.temp }}/keyless-gate-evidence.json" in upload_step
    assert "${{ runner.temp }}/installed-acceptance-evidence.json" in upload_step
    assert "if-no-files-found: warn" in upload_step
    assert "archive: false" not in upload_step


def test_workflow_installs_ffmpeg_from_the_fixed_snapshot_before_the_full_gate():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    media_setup_at = workflow.index("- name: 从固定 snapshot 准备完整门禁媒体能力")
    full_gate_at = workflow.index("- name: 只构建一次并在仅回环网络中执行完整门禁")
    media_setup = workflow[media_setup_at:full_gate_at]

    assert media_setup_at < full_gate_at
    assert 'readonly APT_SNAPSHOT_ID="20260725T000000Z"' in media_setup
    assert '--snapshot "$APT_SNAPSHOT_ID"' in media_setup
    assert "Snapshot: $APT_SNAPSHOT_ID" in media_setup
    assert "URIs: https://archive.ubuntu.com/ubuntu" in media_setup
    assert "URIs: https://security.ubuntu.com/ubuntu" in media_setup
    assert 'Dir::Etc::main=/dev/null' in media_setup
    assert 'Dir::Etc::sourcelist=$APT_STATE/ubuntu.sources' in media_setup
    assert 'Dir::Etc::sourceparts=$APT_STATE/parts' in media_setup
    assert 'Dir::Etc::preferences=$APT_STATE/snapshot.preferences' in media_setup
    assert 'Dir::Etc::preferencesparts=$APT_STATE/parts' in media_setup
    assert 'Dir::State::lists=$APT_STATE/lists' in media_setup
    assert '"${APT_OPTIONS[@]}" policy "$package"' in media_setup
    assert '"${APT_OPTIONS[@]}" madison "$package"' in media_setup
    assert "--no-install-recommends" in media_setup
    assert "packages=(ffmpeg fontconfig fonts-noto-cjk)" in media_setup
    assert 'package_specs+=("$package=$candidate_version")' in media_setup
    assert "--allow-downgrades" in media_setup
    assert "dpkg-query" in media_setup
    assert "ffmpeg -version" in media_setup
    assert "ffprobe -version" in media_setup
    assert "fc-match -f '%{family[0]}\\n'" in media_setup
    assert '[[ "$matched_family" == "Noto Sans CJK SC" ]]' in media_setup


def test_workflow_installs_build_and_gate_tools_only_from_the_hashed_lock():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    tools_at = workflow.index("- name: 从哈希锁安装构建与门禁工具")
    media_setup_at = workflow.index("- name: 从固定 snapshot 准备完整门禁媒体能力")
    tools_step = workflow[tools_at:media_setup_at]

    assert "python -m pip install" in tools_step
    assert "--require-hashes" in tools_step
    assert "--only-binary=:all:" in tools_step
    assert "--no-deps" not in tools_step
    assert "-r requirements-build.lock" in tools_step
    for inline_requirement in (
        '"build==',
        '"pytest==',
        '"setuptools==',
        '"wheel==',
    ):
        assert inline_requirement not in tools_step


def test_workflow_preserves_both_hash_locks_with_the_candidate_evidence():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    validation_loop_start = workflow.index("for source in")
    validation_loop = workflow[
        validation_loop_start : workflow.index("done", validation_loop_start)
    ]

    assert "requirements-build.lock" in validation_loop
    assert "requirements-runtime.lock" in validation_loop
    assert "requirements-build.lock \\" in workflow
    assert '"$trusted_root/candidate/requirements-build.lock"' in workflow
    assert "candidate/requirements-build.lock" in workflow
    assert "candidate/requirements-runtime.lock" in workflow
