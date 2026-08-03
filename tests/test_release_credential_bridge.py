import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BRIDGE = PROJECT_ROOT / "scripts" / "systemd_credential_bridge.py"
RELEASE_TOOL_NAMES = (
    "install-production.sh",
    "run_keyless_gate_network.sh",
    "run_release_gate.py",
    "systemd_credential_bridge.py",
    "validate_installed_delivery.py",
    "validate_release_evidence.py",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_observing_gate(path: Path, capture: Path) -> None:
    source = """\
import json
import os
import resource
import stat
from pathlib import Path
import sys

plan_path = Path(sys.argv[sys.argv.index("--plan") + 1])
capture = Path(__CAPTURE_PATH__)
plan_metadata = plan_path.stat(follow_symlinks=False)
plan_parent_metadata = plan_path.parent.stat(follow_symlinks=False)
plan_root_metadata = plan_path.parent.parent.stat(follow_symlinks=False)
gate_parent_metadata = Path(sys.argv[0]).parent.stat(follow_symlinks=False)
python_parent_metadata = Path(sys.executable).parent.stat(
    follow_symlinks=False
)
credential_fd = int(os.environ["RELEASE_GATE_SYSTEMD_CREDENTIAL_FD"])
metadata = os.fstat(credential_fd)
encoded = os.pread(credential_fd, 4097, 0)
try:
    credential = encoded.decode("utf-8")
except UnicodeDecodeError:
    raise SystemExit(78)
if (
    not credential
    or len(encoded) > 4096
    or "\\0" in credential
    or "\\n" in credential
    or "\\r" in credential
):
    raise SystemExit(78)
capture.write_text(json.dumps({
    "argv": sys.argv,
    "credential": credential,
    "credentials_directory_present": "CREDENTIALS_DIRECTORY" in os.environ,
    "stepfun_environment_present": "STEPFUN_API_KEY" in os.environ,
    "credential_fd_inheritable": os.get_inheritable(credential_fd),
    "credential_file_mode": stat.S_IMODE(metadata.st_mode),
    "credential_filesystem_readonly": bool(
        os.fstatvfs(credential_fd).f_flag & os.ST_RDONLY
    ),
    "host_network_namespace": os.environ.get(
        "RELEASE_GATE_SYSTEMD_HOST_NETNS"
    ),
    "path": os.environ.get("PATH"),
    "core_limit": list(resource.getrlimit(resource.RLIMIT_CORE)),
    "plan_uid": plan_metadata.st_uid,
    "plan_gid": plan_metadata.st_gid,
    "plan_mode": stat.S_IMODE(plan_metadata.st_mode),
    "plan_parent_uid": plan_parent_metadata.st_uid,
    "plan_parent_gid": plan_parent_metadata.st_gid,
    "plan_parent_mode": stat.S_IMODE(plan_parent_metadata.st_mode),
    "plan_root_uid": plan_root_metadata.st_uid,
    "plan_root_gid": plan_root_metadata.st_gid,
    "plan_root_mode": stat.S_IMODE(plan_root_metadata.st_mode),
    "gate_parent_uid": gate_parent_metadata.st_uid,
    "gate_parent_gid": gate_parent_metadata.st_gid,
    "gate_parent_mode": stat.S_IMODE(gate_parent_metadata.st_mode),
    "python_parent_uid": python_parent_metadata.st_uid,
    "python_parent_gid": python_parent_metadata.st_gid,
    "python_parent_mode": stat.S_IMODE(python_parent_metadata.st_mode),
}), encoding="utf-8")
print("gate-completed")
""".replace("__CAPTURE_PATH__", repr(str(capture)))
    path.write_text(
        source,
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_private_plan(
    plan_root: Path,
    gate: Path,
    bridge: Path,
    *,
    slug: str = "private-plan",
    mutate: Callable[[dict[str, Any]], None] | None = None,
    mode: int = 0o440,
    parent_mode: int = 0o710,
) -> Path:
    private = plan_root / slug
    private.mkdir(mode=0o700, exist_ok=True)
    private.chmod(parent_mode)
    release_tools = {name: "0" * 64 for name in RELEASE_TOOL_NAMES}
    release_tools[gate.name] = _sha256(gate)
    release_tools[bridge.name] = _sha256(bridge)
    plan = {
        "schema_version": "release_gate_plan.v1",
        "automation": {"release_tools": release_tools},
        "execution": {
            "credential_bridge": {
                "filename": bridge.name,
                "path": str(bridge),
                "sha256": _sha256(bridge),
            },
            "credential_source": "systemd_credentials",
            "credential_id": "stepfun_api_key",
        },
    }
    if mutate is not None:
        mutate(plan)
    destination = private / "plan.json"
    destination.unlink(missing_ok=True)
    destination.write_text(
        json.dumps(plan, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    destination.chmod(mode)
    return destination


def _trusted_python(tmp_path: Path, *, parent_mode: int = 0o710) -> Path:
    runtime = tmp_path / "python-runtime"
    runtime.mkdir(mode=0o700, exist_ok=True)
    runtime.chmod(parent_mode)
    destination = runtime / "trusted-python"
    shutil.copy2(Path(sys.executable).resolve(), destination)
    destination.chmod(0o755)
    return destination


def _run_bridge(
    tmp_path: Path,
    credential_directory: Path,
    *,
    preexisting_credential: str | None = None,
    standard_input: str | None = None,
    bridge_prefix: tuple[str, ...] = ("--",),
    readonly_mount: bool = True,
    command_override: tuple[str, ...] | None = None,
    plan_mutator: Callable[[dict[str, Any]], None] | None = None,
    plan_mode: int = 0o440,
    plan_parent_mode: int = 0o710,
    gate_action: str = "execute",
    operator_uid: str = "1000",
    operator_gid: str = "1000",
    trusted_context: bool = True,
    symlink_python: bool = False,
    release_tool_parent_mode: int = 0o710,
    python_parent_mode: int = 0o710,
    separate_gate_directory: bool = False,
    namespace_gid: str = "1000",
    trusted_plan_root_mode: int = 0o711,
    ancestor_floor_mode: int | None = None,
    plan_location: str = "trusted",
    plan_slug: str = "private-plan",
    systemd_unit_slug: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    if ancestor_floor_mode is not None:
        tmp_path.chmod(ancestor_floor_mode)
    release_tool_directory = tmp_path / "release-tools"
    release_tool_directory.mkdir(mode=0o700, exist_ok=True)
    release_tool_directory.chmod(release_tool_parent_mode)
    bridge = release_tool_directory / BRIDGE.name
    shutil.copy2(BRIDGE, bridge)
    bridge.chmod(0o755)
    gate_directory = release_tool_directory
    if separate_gate_directory:
        gate_directory = tmp_path / "alternate-release-tools"
        gate_directory.mkdir(mode=0o700, exist_ok=True)
    gate = gate_directory / "run_release_gate.py"
    capture = tmp_path / "capture.json"
    _write_observing_gate(gate, capture)
    trusted_plan_root = tmp_path / "trusted-plans"
    trusted_plan_root.mkdir(mode=0o700, exist_ok=True)
    trusted_plan_root.chmod(trusted_plan_root_mode)
    if plan_location == "trusted":
        plan_root = trusted_plan_root
    elif plan_location == "outside":
        plan_root = tmp_path / "untrusted-plans"
        plan_root.mkdir(mode=0o700, exist_ok=True)
        plan_root.chmod(0o711)
    elif plan_location == "nested":
        plan_root = trusted_plan_root / "nested"
        plan_root.mkdir(mode=0o700, exist_ok=True)
        plan_root.chmod(0o711)
    else:
        raise ValueError("unknown plan location")
    plan = _write_private_plan(
        plan_root,
        gate,
        bridge,
        slug=plan_slug,
        mutate=plan_mutator,
        mode=plan_mode,
        parent_mode=plan_parent_mode,
    )
    python = _trusted_python(tmp_path, parent_mode=python_parent_mode)
    command_python = python
    if symlink_python:
        command_python = python.parent / "trusted-python-alias"
        command_python.symlink_to(python)
    environment = os.environ.copy()
    environment.pop("STEPFUN_API_KEY", None)
    if preexisting_credential is not None:
        environment["STEPFUN_API_KEY"] = preexisting_credential
    environment["CREDENTIALS_DIRECTORY"] = str(credential_directory)
    if credential_directory.is_dir():
        credential_directory.chmod(0o700)
        credential = credential_directory / "stepfun_api_key"
        if credential.is_file() and not credential.is_symlink():
            credential.chmod(0o400)
    bridge_arguments = [
        "--operator-uid",
        operator_uid,
        "--operator-gid",
        operator_gid,
        *bridge_prefix,
        *(
            command_override
            if command_override is not None
            else (
                str(command_python),
                "-I",
                str(gate),
                gate_action,
                "--plan",
                str(plan),
            )
        ),
    ]
    trusted_context_overrides = ""
    if trusted_context:
        resolved_systemd_unit_slug = (
            plan_slug if systemd_unit_slug is None else systemd_unit_slug
        )
        trusted_context_overrides = f"""\
module._trusted_systemd_credential_directory = (
    lambda environment, _action: Path(environment["CREDENTIALS_DIRECTORY"])
)
module._systemd_unit_slug = (
    lambda _directory, _action: {resolved_systemd_unit_slug!r}
)
module._verify_network_context = lambda _action: "net:[100]"
module._drop_privileges = lambda _uid, _gid: None
"""
    bootstrap = f"""
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "_credential_bridge_under_test",
    {str(bridge)!r},
)
if spec is None or spec.loader is None:
    raise SystemExit(97)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module._TRUSTED_PLAN_ROOT = Path({str(trusted_plan_root)!r})
module._TRUSTED_PLAN_ANCESTOR_FLOOR = Path({str(tmp_path)!r})
{trusted_context_overrides}
raise SystemExit(module.main())
"""
    command = [str(python), "-I", "-c", bootstrap, *bridge_arguments]
    if readonly_mount:
        command = [
            "unshare",
            "--user",
            "--map-user=0",
            f"--map-group={namespace_gid}",
            "--mount",
            "sh",
            "-c",
            (
                'mount --bind "$1" "$1" && '
                'mount -o remount,bind,ro "$1" && '
                'shift && exec "$@"'
            ),
            "sh",
            str(credential_directory),
            *command,
        ]
    completed = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        input=standard_input,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed, capture


@pytest.mark.parametrize("gate_action", ("execute", "rerun"))
def test_bridge_inherits_only_the_verified_systemd_credential_descriptor(
    tmp_path,
    gate_action,
):
    credential_canary = "credential-canary-only-for-child-environment"
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    (credential_directory / "stepfun_api_key").write_text(
        credential_canary,
        encoding="utf-8",
    )
    completed, capture = _run_bridge(
        tmp_path,
        credential_directory,
        gate_action=gate_action,
    )

    assert completed.returncode == 0, completed.stderr
    observed = json.loads(capture.read_text(encoding="utf-8"))
    assert observed["credential"] == credential_canary
    assert observed["credentials_directory_present"] is False
    assert observed["stepfun_environment_present"] is False
    assert observed["credential_fd_inheritable"] is True
    assert observed["credential_file_mode"] == 0o400
    assert observed["credential_filesystem_readonly"] is True
    assert observed["host_network_namespace"] == "net:[100]"
    assert observed["path"] == "/usr/sbin:/usr/bin:/sbin:/bin"
    assert observed["core_limit"] == [0, 0]
    assert observed["plan_uid"] == 0
    assert observed["plan_gid"] == 1000
    assert observed["plan_mode"] == 0o440
    assert observed["plan_parent_uid"] == 0
    assert observed["plan_parent_gid"] == 1000
    assert observed["plan_parent_mode"] == 0o710
    assert observed["plan_root_uid"] == 0
    assert observed["plan_root_gid"] == 1000
    assert observed["plan_root_mode"] == 0o711
    assert observed["gate_parent_uid"] == 0
    assert observed["gate_parent_gid"] == 1000
    assert observed["gate_parent_mode"] == 0o710
    assert observed["python_parent_uid"] == 0
    assert observed["python_parent_gid"] == 1000
    assert observed["python_parent_mode"] == 0o710
    assert observed["argv"] == [
        str(tmp_path / "release-tools" / "run_release_gate.py"),
        gate_action,
        "--plan",
        str(tmp_path / "trusted-plans" / "private-plan" / "plan.json"),
    ]
    assert credential_canary not in "\0".join(completed.args)
    assert credential_canary not in "\0".join(observed["argv"])
    assert credential_canary not in completed.stdout
    assert credential_canary not in completed.stderr
    assert completed.stdout == "gate-completed\n"


def test_bridge_rejects_a_gate_outside_the_bridge_directory(tmp_path):
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    (credential_directory / "stepfun_api_key").write_text(
        "separate-gate-directory-credential",
        encoding="utf-8",
    )

    completed, capture = _run_bridge(
        tmp_path,
        credential_directory,
        separate_gate_directory=True,
    )

    assert completed.returncode == 78
    assert not capture.exists()
    assert completed.stdout == ""
    assert completed.stderr == "固定命令未绑定受信发布工具\n"


@pytest.mark.parametrize(
    ("parent_argument", "parent_mode"),
    (
        ("release_tool_parent_mode", 0o730),
        ("release_tool_parent_mode", 0o712),
        ("python_parent_mode", 0o730),
        ("python_parent_mode", 0o712),
    ),
)
def test_bridge_rejects_a_writable_trusted_tool_parent(
    tmp_path,
    parent_argument,
    parent_mode,
):
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    (credential_directory / "stepfun_api_key").write_text(
        "writable-tool-parent-credential",
        encoding="utf-8",
    )

    completed, capture = _run_bridge(
        tmp_path,
        credential_directory,
        **{parent_argument: parent_mode},
    )

    assert completed.returncode == 78
    assert not capture.exists()
    assert completed.stdout == ""
    assert completed.stderr == "固定命令未绑定受信发布工具\n"


def test_bridge_rejects_a_user_namespace_fake_systemd_credential(tmp_path):
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    (credential_directory / "stepfun_api_key").write_text(
        "user-namespace-fake-credential",
        encoding="utf-8",
    )

    completed, capture = _run_bridge(
        tmp_path,
        credential_directory,
        trusted_context=False,
    )

    assert completed.returncode == 78
    assert not capture.exists()
    assert completed.stdout == ""
    assert completed.stderr == "systemd 系统服务上下文不可用\n"


@pytest.mark.parametrize("plan_mode", (0o444, 0o600))
def test_bridge_rejects_a_release_plan_with_noncanonical_mode(
    tmp_path,
    plan_mode,
):
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    (credential_directory / "stepfun_api_key").write_text(
        "operator-writable-plan-credential",
        encoding="utf-8",
    )

    completed, capture = _run_bridge(
        tmp_path,
        credential_directory,
        plan_mode=plan_mode,
    )

    assert completed.returncode == 78
    assert not capture.exists()
    assert completed.stdout == ""
    assert completed.stderr == "固定命令未绑定受信发布工具\n"


@pytest.mark.parametrize("plan_parent_mode", (0o700, 0o750, 0o711, 0o730))
def test_bridge_rejects_a_release_plan_parent_with_noncanonical_mode(
    tmp_path,
    plan_parent_mode,
):
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    (credential_directory / "stepfun_api_key").write_text(
        "noncanonical-plan-parent-credential",
        encoding="utf-8",
    )

    completed, capture = _run_bridge(
        tmp_path,
        credential_directory,
        plan_parent_mode=plan_parent_mode,
    )

    assert completed.returncode == 78
    assert not capture.exists()
    assert completed.stdout == ""
    assert completed.stderr == "固定命令未绑定受信发布工具\n"


def test_bridge_rejects_a_release_plan_outside_the_operator_group(tmp_path):
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    (credential_directory / "stepfun_api_key").write_text(
        "wrong-plan-group-credential",
        encoding="utf-8",
    )

    completed, capture = _run_bridge(
        tmp_path,
        credential_directory,
        namespace_gid="0",
    )

    assert completed.returncode == 78
    assert not capture.exists()
    assert completed.stdout == ""
    assert completed.stderr == "固定命令未绑定受信发布工具\n"


@pytest.mark.parametrize("plan_location", ("outside", "nested"))
def test_bridge_rejects_a_plan_outside_the_fixed_trusted_location(
    tmp_path,
    plan_location,
):
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    (credential_directory / "stepfun_api_key").write_text(
        "untrusted-plan-location-credential",
        encoding="utf-8",
    )

    completed, capture = _run_bridge(
        tmp_path,
        credential_directory,
        plan_location=plan_location,
    )

    assert completed.returncode == 78
    assert not capture.exists()
    assert completed.stdout == ""
    assert completed.stderr == "固定命令未绑定受信发布工具\n"


@pytest.mark.parametrize(
    "plan_slug",
    ("Candidate", "candidate_slug", ".hidden", "a" * 65),
)
def test_bridge_rejects_a_noncanonical_plan_slug(tmp_path, plan_slug):
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    (credential_directory / "stepfun_api_key").write_text(
        "noncanonical-plan-slug-credential",
        encoding="utf-8",
    )

    completed, capture = _run_bridge(
        tmp_path,
        credential_directory,
        plan_slug=plan_slug,
    )

    assert completed.returncode == 78
    assert not capture.exists()
    assert completed.stdout == ""
    assert completed.stderr == "固定命令未绑定受信发布工具\n"


def test_bridge_rejects_a_plan_for_another_systemd_unit_slug(tmp_path):
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    (credential_directory / "stepfun_api_key").write_text(
        "wrong-systemd-unit-slug-credential",
        encoding="utf-8",
    )

    completed, capture = _run_bridge(
        tmp_path,
        credential_directory,
        systemd_unit_slug="another-candidate",
    )

    assert completed.returncode == 78
    assert not capture.exists()
    assert completed.stdout == ""
    assert completed.stderr == "固定命令未绑定受信发布工具\n"


@pytest.mark.parametrize(
    ("ancestor_argument", "ancestor_mode"),
    (
        ("trusted_plan_root_mode", 0o731),
        ("trusted_plan_root_mode", 0o713),
        ("ancestor_floor_mode", 0o730),
        ("ancestor_floor_mode", 0o702),
    ),
)
def test_bridge_rejects_a_writable_trusted_plan_ancestor(
    tmp_path,
    ancestor_argument,
    ancestor_mode,
):
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    (credential_directory / "stepfun_api_key").write_text(
        "writable-plan-ancestor-credential",
        encoding="utf-8",
    )

    completed, capture = _run_bridge(
        tmp_path,
        credential_directory,
        **{ancestor_argument: ancestor_mode},
    )

    assert completed.returncode == 78
    assert not capture.exists()
    assert completed.stdout == ""
    assert completed.stderr == "固定命令未绑定受信发布工具\n"


@pytest.mark.parametrize("operator_uid", ("0", "01", "-1", "4294967295"))
def test_bridge_rejects_a_noncanonical_operator_identity(
    tmp_path,
    operator_uid,
):
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    (credential_directory / "stepfun_api_key").write_text(
        "operator-identity-credential",
        encoding="utf-8",
    )

    completed, capture = _run_bridge(
        tmp_path,
        credential_directory,
        operator_uid=operator_uid,
    )

    assert completed.returncode == 78
    assert not capture.exists()
    assert completed.stderr == "发布操作员身份不合法\n"


def test_bridge_requires_the_resolved_regular_python_interpreter(tmp_path):
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    (credential_directory / "stepfun_api_key").write_text(
        "python-alias-credential",
        encoding="utf-8",
    )

    completed, capture = _run_bridge(
        tmp_path,
        credential_directory,
        symlink_python=True,
    )

    assert completed.returncode == 78
    assert not capture.exists()
    assert completed.stderr == "固定命令未绑定受信发布工具\n"


def test_bridge_rejects_a_writable_fake_credential_directory(tmp_path):
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    (credential_directory / "stepfun_api_key").write_text(
        "writable-directory-canary",
        encoding="utf-8",
    )

    completed, capture = _run_bridge(
        tmp_path,
        credential_directory,
        readonly_mount=False,
    )

    assert completed.returncode != 0
    assert not capture.exists()
    assert "writable-directory-canary" not in completed.stdout + completed.stderr


def test_bridge_does_not_fall_back_to_stdin_when_credential_is_missing(
    tmp_path,
):
    stdin_canary = "stdin-credential-canary"
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()

    completed, capture = _run_bridge(
        tmp_path,
        credential_directory,
        standard_input=stdin_canary,
    )

    assert completed.returncode != 0
    assert not capture.exists()
    assert stdin_canary not in completed.stdout
    assert stdin_canary not in completed.stderr


def test_bridge_rejects_a_symbolic_link_credential_without_reading_target(
    tmp_path,
):
    target_canary = "symbolic-link-target-canary"
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    target = tmp_path / "outside-credential"
    target.write_text(target_canary, encoding="utf-8")
    (credential_directory / "stepfun_api_key").symlink_to(target)

    completed, capture = _run_bridge(tmp_path, credential_directory)

    assert completed.returncode != 0
    assert not capture.exists()
    assert target_canary not in completed.stdout
    assert target_canary not in completed.stderr


def test_bridge_rejects_a_non_regular_credential_file(tmp_path):
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    (credential_directory / "stepfun_api_key").mkdir()

    completed, capture = _run_bridge(tmp_path, credential_directory)

    assert completed.returncode != 0
    assert not capture.exists()


def test_bridge_rejects_a_credential_larger_than_four_kibibytes(tmp_path):
    oversized_canary = "oversized-credential-canary-" + ("x" * 4096)
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    (credential_directory / "stepfun_api_key").write_text(
        oversized_canary,
        encoding="utf-8",
    )

    completed, capture = _run_bridge(tmp_path, credential_directory)

    assert completed.returncode != 0
    assert not capture.exists()
    assert "oversized-credential-canary" not in completed.stdout
    assert "oversized-credential-canary" not in completed.stderr


def test_bridge_rejects_a_credential_containing_a_line_break(tmp_path):
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    (credential_directory / "stepfun_api_key").write_text(
        "line-break-canary\n",
        encoding="utf-8",
    )

    completed, capture = _run_bridge(tmp_path, credential_directory)

    assert completed.returncode != 0
    assert not capture.exists()
    assert "line-break-canary" not in completed.stdout + completed.stderr


def test_bridge_rejects_a_preexisting_stepfun_environment_variable(
    tmp_path,
):
    file_canary = "credential-file-canary"
    preexisting_canary = "preexisting-environment-canary"
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    (credential_directory / "stepfun_api_key").write_text(
        file_canary,
        encoding="utf-8",
    )

    completed, capture = _run_bridge(
        tmp_path,
        credential_directory,
        preexisting_credential=preexisting_canary,
    )

    assert completed.returncode != 0
    assert not capture.exists()
    assert file_canary not in completed.stdout + completed.stderr
    assert preexisting_canary not in completed.stdout + completed.stderr


def test_bridge_rejects_credential_arguments_and_json(tmp_path):
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    (credential_directory / "stepfun_api_key").write_text(
        "valid-file-credential",
        encoding="utf-8",
    )

    rejected_sources = (
        (
            "argument-credential-canary",
            ("--stepfun-api-key", "argument-credential-canary", "--"),
        ),
        (
            "json-credential-canary",
            (
                "--credential-json",
                json.dumps(
                    {"stepfun_api_key": "json-credential-canary"}
                ),
                "--",
            ),
        ),
    )
    for source_canary, bridge_prefix in rejected_sources:
        completed, capture = _run_bridge(
            tmp_path,
            credential_directory,
            bridge_prefix=bridge_prefix,
        )

        assert completed.returncode != 0
        assert not capture.exists()
        assert source_canary not in completed.stdout
        assert source_canary not in completed.stderr


def test_bridge_rejects_an_arbitrary_absolute_program(tmp_path):
    credential_canary = "arbitrary-program-credential-canary"
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    (credential_directory / "stepfun_api_key").write_text(
        credential_canary,
        encoding="utf-8",
    )

    completed, capture = _run_bridge(
        tmp_path,
        credential_directory,
        command_override=("/usr/bin/true",),
    )

    assert completed.returncode == 78
    assert not capture.exists()
    assert completed.stdout == ""
    assert completed.stderr == "固定命令未绑定受信发布工具\n"
    assert credential_canary not in completed.stdout + completed.stderr


@pytest.mark.parametrize("drift", ("gate", "bridge"))
def test_bridge_rejects_release_tool_digest_drift(tmp_path, drift):
    credential_canary = f"{drift}-digest-credential-canary"
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    (credential_directory / "stepfun_api_key").write_text(
        credential_canary,
        encoding="utf-8",
    )

    def mutate(plan):
        if drift == "gate":
            plan["automation"]["release_tools"]["run_release_gate.py"] = (
                "f" * 64
            )
        else:
            plan["automation"]["release_tools"][
                "systemd_credential_bridge.py"
            ] = "f" * 64
            plan["execution"]["credential_bridge"]["sha256"] = "f" * 64

    completed, capture = _run_bridge(
        tmp_path,
        credential_directory,
        plan_mutator=mutate,
    )

    assert completed.returncode == 78
    assert not capture.exists()
    assert completed.stdout == ""
    assert completed.stderr == "固定命令未绑定受信发布工具\n"
    assert credential_canary not in completed.stdout + completed.stderr
