import hashlib
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_INSTALLER = PROJECT_ROOT / "scripts" / "install-production.sh"
INSTALLER = PROJECT_ROOT / "tests" / "support" / "run_production_installer.py"
APPLICATION_VERSION = "4.7.0"
SNAPSHOT_ID = "20260725T000000Z"
SYSTEM_PACKAGES = {
    "ca-certificates": "20240203",
    "ffmpeg": "7:6.1.1-3ubuntu5",
    "fontconfig": "2.15.0-1.1ubuntu2",
    "fonts-noto-cjk": "1:20230817+repack1-3",
    "python3.12": "3.12.3-1ubuntu0.8",
    "python3.12-venv": "3.12.3-1ubuntu0.8",
}
SYSTEM_PACKAGE_INVENTORY = {
    **SYSTEM_PACKAGES,
    "libass9": "1:0.17.1-1build1",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _write_candidate_wheel(
    directory: Path,
    *,
    version: str = APPLICATION_VERSION,
    payload: str = "locked-candidate",
) -> Path:
    wheel_version = version.replace("-", "_")
    wheel = directory / f"video_auto_editor-{wheel_version}-py3-none-any.whl"
    dist_info = f"video_auto_editor-{wheel_version}.dist-info"
    with ZipFile(wheel, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            "video_auto_editor/__init__.py",
            f'__version__ = "{version}"\nPAYLOAD = {payload!r}\n',
        )
        archive.writestr(
            "video_auto_editor/cli.py",
            "def main():\n"
            f'    print("video-auto-editor {version}")\n'
            "    return 0\n",
        )
        archive.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.4\n"
            "Name: video-auto-editor\n"
            f"Version: {version}\n"
            "Requires-Python: <3.13,>=3.12.3\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\n"
            "Generator: production-installation-test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
        archive.writestr(
            f"{dist_info}/entry_points.txt",
            "[console_scripts]\n"
            "video-auto-editor = video_auto_editor.cli:main\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")
    return wheel


def _write_runtime_dependency_wheel(
    directory: Path,
    *,
    requires_dist: str | None = None,
) -> Path:
    wheel = directory / "sample_dependency-1.0-py3-none-any.whl"
    dist_info = "sample_dependency-1.0.dist-info"
    with ZipFile(wheel, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("sample_dependency.py", "VALUE = 'locked-wheelhouse'\n")
        metadata = (
            "Metadata-Version: 2.4\n"
            "Name: sample-dependency\n"
            "Version: 1.0\n"
            "Requires-Python: >=3.12.3,<3.13\n"
        )
        if requires_dist is not None:
            metadata += f"Requires-Dist: {requires_dist}\n"
        archive.writestr(f"{dist_info}/METADATA", metadata)
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")
    return wheel


def _write_fake_system_commands(directory: Path, trace: Path) -> Path:
    fake_bin = directory / "fake-bin"
    fake_bin.mkdir()
    font_file = directory / "NotoSansCJK-Regular.ttc"
    font_file.write_bytes(b"test-font")
    apt_sources_trace = trace.with_suffix(".sources")

    _write_executable(
        fake_bin / "dpkg",
        "#!/bin/sh\n"
        "if [ \"${1-}\" = \"--print-architecture\" ]; then "
        "printf 'amd64\\n'; exit 0; fi\n"
        "exit 64\n",
    )
    _write_executable(
        fake_bin / "apt-get",
        "#!/bin/sh\n"
        "source_file=\n"
        "for argument do\n"
        "  case \"$argument\" in\n"
        "    Dir::Etc::sourcelist=*) source_file=${argument#*=};;\n"
        "  esac\n"
        "done\n"
        f"if [ -n \"$source_file\" ] && [ ! -e {apt_sources_trace!s} ]; then\n"
        f"  /bin/cp -- \"$source_file\" {apt_sources_trace!s}\n"
        "fi\n"
        "preference_file=\n"
        "for argument do\n"
        "  case \"$argument\" in\n"
        "    Dir::Etc::preferences=*) preference_file=${argument#*=};;\n"
        "  esac\n"
        "done\n"
        f"if [ -n \"$preference_file\" ] && "
        f"[ ! -e {trace.with_suffix('.preferences')!s} ]; then\n"
        f"  /bin/cp -- \"$preference_file\" "
        f"{trace.with_suffix('.preferences')!s}\n"
        "fi\n"
        f"printf 'apt-env\\t%s\\n' \"${{DEBIAN_FRONTEND-}}\" >> {trace!s}\n"
        f"printf 'apt-get' >> {trace!s}\n"
        f"printf '\\t%s' \"$@\" >> {trace!s}\n"
        f"printf '\\n' >> {trace!s}\n",
    )
    policy_cases = "".join(
        f"  {name}) printf '  Candidate: {version}\\n';;\n"
        for name, version in SYSTEM_PACKAGES.items()
    )
    madison_cases = "".join(
        f"  {name}) printf '{name} | {version} | snapshot\\n';;\n"
        for name, version in SYSTEM_PACKAGES.items()
    )
    _write_executable(
        fake_bin / "apt-cache",
        "#!/bin/sh\n"
        f"printf 'apt-cache' >> {trace!s}\n"
        f"printf '\\t%s' \"$@\" >> {trace!s}\n"
        f"printf '\\n' >> {trace!s}\n"
        "previous=\n"
        "operation=\n"
        "package=\n"
        "for argument do\n"
        "  case \"$previous\" in\n"
        "    policy|madison) operation=$previous; package=$argument; break;;\n"
        "  esac\n"
        "  previous=$argument\n"
        "done\n"
        "case \"$operation\" in\n"
        "  policy)\n"
        "    case \"$package\" in\n"
        f"{policy_cases}"
        "      *) exit 64;;\n"
        "    esac\n"
        "    ;;\n"
        "  madison)\n"
        "    case \"$package\" in\n"
        f"{madison_cases}"
        "      *) exit 64;;\n"
        "    esac\n"
        "    ;;\n"
        "  *) exit 64;;\n"
        "esac\n",
    )
    inventory_lines = "".join(
        f"{name}\\t{version}\\tii \\n"
        for name, version in SYSTEM_PACKAGE_INVENTORY.items()
    )
    installed_version_cases = "".join(
        f"  {name}) printf '{version}\\n';;\n"
        for name, version in SYSTEM_PACKAGES.items()
    )
    delayed_drift_marker = trace.with_suffix(".dpkg-delayed-drift")
    _write_executable(
        fake_bin / "dpkg-query",
        "#!/bin/sh\n"
        f"printf 'dpkg-query' >> {trace!s}\n"
        f"printf '\\t%s' \"$@\" >> {trace!s}\n"
        f"printf '\\n' >> {trace!s}\n"
        "case \" $* \" in\n"
        "  *'${db:Status-Abbrev}'*)\n"
        f"    printf '{inventory_lines}'\n"
        "    ;;\n"
        "  *)\n"
        "    for package do :; done\n"
        "    if [ \"${FAKE_DPKG_DRIFT_PACKAGE-}\" = \"$package\" ]; then\n"
        "      printf '99:drifted\\n'\n"
        "      exit 0\n"
        "    fi\n"
        "    if [ \"${FAKE_DPKG_DELAYED_DRIFT_PACKAGE-}\" = \"$package\" ]; "
        "then\n"
        f"      if [ -e {delayed_drift_marker!s} ]; then\n"
        "        printf '99:drifted\\n'\n"
        "        exit 0\n"
        "      fi\n"
        f"      : > {delayed_drift_marker!s}\n"
        "    fi\n"
        "    case \"$package\" in\n"
        f"{installed_version_cases}"
        "      *) exit 64;;\n"
        "    esac\n"
        "    ;;\n"
        "esac\n",
    )
    _write_executable(
        fake_bin / "ffmpeg",
        "#!/bin/sh\n"
        f"printf 'ffmpeg' >> {trace!s}\n"
        f"printf '\\t%s' \"$@\" >> {trace!s}\n"
        f"printf '\\n' >> {trace!s}\n"
        "case \" $* \" in\n"
        "  *' -version '*) printf 'ffmpeg version 6.1.1-3ubuntu5\\n';;\n"
        "  *' -filters '*)\n"
        "    printf ' T.. subtitles V->V Render subtitles\\n'\n"
        "    if [ \"${FAKE_LARGE_FILTER_LISTING-}\" = 1 ]; then\n"
        "      /bin/dd if=/dev/zero bs=4096 count=64 2>/dev/null "
        "| /usr/bin/tr '\\000' x\n"
        "      printf '\\n'\n"
        "    fi\n"
        "    ;;\n"
        "  *' -encoders '*) printf ' V....D libx264\\n A..... aac\\n';;\n"
        "  *) for output do :; done; : > \"$output\";;\n"
        "esac\n",
    )
    _write_executable(
        fake_bin / "ffprobe",
        "#!/bin/sh\n"
        f"printf 'ffprobe' >> {trace!s}\n"
        f"printf '\\t%s' \"$@\" >> {trace!s}\n"
        f"printf '\\n' >> {trace!s}\n"
        "case \" $* \" in\n"
        "  *' -version '*) printf 'ffprobe version 6.1.1-3ubuntu5\\n';;\n"
        "  *) printf '%s\\n' "
        "'{\"format\":{\"format_name\":\"mov,mp4\","
        "\"duration\":\"1.000\"},\"streams\":["
        "{\"codec_type\":\"video\"},{\"codec_type\":\"audio\"}]}' ;;\n"
        "esac\n",
    )
    _write_executable(
        fake_bin / "fc-list",
        "#!/bin/sh\nexit 0\n",
    )
    _write_executable(
        fake_bin / "fc-match",
        "#!/bin/sh\n"
        f"printf 'Noto Sans CJK SC\\n{font_file!s}\\n'\n",
    )
    return fake_bin


def _run_installer(
    *,
    tmp_path: Path,
    wheel: Path,
    wheelhouse: Path,
    runtime_lock: Path,
    prefix: Path,
    fake_bin: Path,
    os_release: Path,
    wheel_sha256: str | None = None,
    runtime_lock_sha256: str | None = None,
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "INSTALL_PRODUCTION_TEST_OS_RELEASE_FILE": str(os_release),
        **(extra_environment or {}),
    }
    return subprocess.run(
        (
            str(INSTALLER),
            "--wheel",
            str(wheel),
            "--wheel-sha256",
            wheel_sha256 or _sha256(wheel),
            "--wheelhouse",
            str(wheelhouse),
            "--runtime-lock",
            str(runtime_lock),
            "--runtime-lock-sha256",
            runtime_lock_sha256 or _sha256(runtime_lock),
            "--apt-snapshot-id",
            SNAPSHOT_ID,
            "--prefix",
            str(prefix),
        ),
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_production_entrypoint_fixes_platform_identity_and_effective_uid():
    source = PRODUCTION_INSTALLER.read_text(encoding="utf-8")

    assert source.startswith("#!/bin/bash -p\n")
    assert "INSTALL_PRODUCTION_TEST" not in source
    assert "INSTALL_PRODUCTION_OS_RELEASE_FILE" not in source
    assert "exec /usr/bin/env -i" in source
    assert 'PATH="${TRUSTED_COMMAND_PATH}"' in source
    assert "LC_ALL=C.UTF-8" in source
    assert "LANG=C.UTF-8" in source
    assert "LANGUAGE=C.UTF-8" in source
    assert "OPENSSL_CONF" not in source
    assert "PERL5OPT" not in source
    assert (
        'install_production_main /etc/os-release "${EUID}" "${EUID}" "$@"'
        in source
    )
    assert "$(id -u)" not in source


def test_production_entrypoint_does_not_search_inherited_path_for_bash(
    tmp_path,
):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    marker = tmp_path / "hijacked"
    _write_executable(
        fake_bin / "bash",
        "#!/bin/sh\n"
        f"printf hijacked > {shlex.quote(str(marker))}\n"
        "exit 91\n",
    )

    completed = subprocess.run(
        (str(PRODUCTION_INSTALLER), "--help"),
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert not marker.exists()


def test_production_entrypoint_ignores_bash_env(tmp_path):
    marker = tmp_path / "bash-env-ran"
    bash_env = tmp_path / "bash-env"
    bash_env.write_text(
        f"printf hijacked > {shlex.quote(str(marker))}\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        (str(PRODUCTION_INSTALLER), "--help"),
        cwd=tmp_path,
        env={**os.environ, "BASH_ENV": str(bash_env)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert not marker.exists()


def test_installer_uses_isolated_python_bootstraps():
    source = PRODUCTION_INSTALLER.read_text(encoding="utf-8")

    assert "python3.12 - " not in source
    assert "python3.12 - <<" not in source
    assert "python3.12 -m venv" not in source
    assert '/venv/bin/python" -m pip' not in source
    assert '/venv/bin/python" - ' not in source


def test_locked_candidate_installs_and_becomes_current_only_after_readiness(
    tmp_path,
):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts)
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text(
        "# 当前生产运行时没有第三方 Python 依赖。\n",
        encoding="utf-8",
    )
    prefix = tmp_path / "installation"
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    os_release_target = tmp_path / "usr-lib-os-release"
    os_release_target.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )
    os_release = tmp_path / "os-release"
    os_release.symlink_to(os_release_target)

    completed = subprocess.run(
        (
            str(INSTALLER),
            "--wheel",
            str(wheel),
            "--wheel-sha256",
            _sha256(wheel),
            "--wheelhouse",
            str(wheelhouse),
            "--runtime-lock",
            str(runtime_lock),
            "--runtime-lock-sha256",
            _sha256(runtime_lock),
            "--apt-snapshot-id",
            SNAPSHOT_ID,
            "--prefix",
            str(prefix),
        ),
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "INSTALL_PRODUCTION_TEST_OS_RELEASE_FILE": str(os_release),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    version_directory = prefix / "versions" / APPLICATION_VERSION
    assert (prefix / "current").readlink() == Path(
        f"versions/{APPLICATION_VERSION}"
    )
    assert (version_directory / "venv" / "bin" / "video-auto-editor").is_file()
    manifest_path = version_directory / "installation-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest == {
        "application": {
            "name": "video-auto-editor",
            "version": APPLICATION_VERSION,
            "wheel": {
                "filename": wheel.name,
                "sha256": _sha256(wheel),
            },
        },
        "apt_snapshot_id": SNAPSHOT_ID,
        "environment": {
            "ffmpeg_version": "6.1.1-3ubuntu5",
            "ffprobe_version": "6.1.1-3ubuntu5",
            "font_family": "Noto Sans CJK SC",
            "font_file": str(tmp_path / "NotoSansCJK-Regular.ttc"),
        },
        "installation_prefix": str(prefix),
        "platform": {
            "architecture": "amd64",
            "operating_system": "ubuntu",
            "operating_system_version": "24.04",
        },
        "python": {
            "implementation": "CPython",
            "version": "3.12.3",
        },
        "runtime_lock": {
            "filename": runtime_lock.name,
            "sha256": _sha256(runtime_lock),
        },
        "schema_version": "production-installation-manifest.v1",
        "snapshot_packages": SYSTEM_PACKAGES,
        "system_packages": SYSTEM_PACKAGE_INVENTORY,
        "wheelhouse": [],
    }
    ready = json.loads((version_directory / "READY").read_text(encoding="utf-8"))
    assert ready == {
        "installation_manifest_sha256": _sha256(manifest_path),
        "schema_version": "production-installation-ready.v1",
    }
    assert str(manifest_path) in completed.stdout
    commands = trace.read_text(encoding="utf-8").splitlines()
    apt_commands = [line for line in commands if line.startswith("apt-get\t")]
    assert len(apt_commands) == 2
    assert all(f"--snapshot\t{SNAPSHOT_ID}" in line for line in apt_commands)
    update_command = next(line for line in apt_commands if "\tupdate" in line)
    assert "\t--error-on=any\tupdate" in update_command
    install_command = next(line for line in apt_commands if "\tinstall" in line)
    assert "\t--allow-downgrades" in install_command
    assert "\t--no-remove" in install_command
    for package, version in SYSTEM_PACKAGES.items():
        assert f"\t{package}={version}" in install_command
    assert all("\tDir::State::lists=" in line for line in apt_commands)
    assert all("\tDir::Etc::main=/dev/null" in line for line in apt_commands)
    assert all("\tDir::Etc::parts=" in line for line in apt_commands)
    assert all("\tDir::Etc::preferences=" in line for line in apt_commands)
    assert all("\tDir::Etc::sourceparts=-" not in line for line in apt_commands)
    assert sum(line.startswith("apt-cache\t") for line in commands) == (
        len(SYSTEM_PACKAGES) * 2
    )
    assert commands.count("apt-env\tnoninteractive") == 2
    assert trace.with_suffix(".sources").read_text(encoding="utf-8") == (
        "Types: deb\n"
        "URIs: http://archive.ubuntu.com/ubuntu\n"
        "Suites: noble noble-updates\n"
        "Components: main universe\n"
        "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n"
        f"Snapshot: {SNAPSHOT_ID}\n"
        "\n"
        "Types: deb\n"
        "URIs: http://security.ubuntu.com/ubuntu\n"
        "Suites: noble-security\n"
        "Components: main universe\n"
        "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n"
        f"Snapshot: {SNAPSHOT_ID}\n"
    )
    assert trace.with_suffix(".preferences").read_text(encoding="utf-8") == (
        "Package: *\n"
        "Pin: release o=Ubuntu,n=noble\n"
        "Pin-Priority: 1001\n"
        "\n"
        "Package: *\n"
        "Pin: release o=Ubuntu,n=noble-updates\n"
        "Pin-Priority: 1001\n"
        "\n"
        "Package: *\n"
        "Pin: release o=Ubuntu,n=noble-security\n"
        "Pin-Priority: 1001\n"
    )


def test_default_acl_on_the_prefix_parent_cannot_relax_installed_records(
    tmp_path,
):
    """父目录带默认 ACL 时 umask 失效，安装记录仍不得组或其他用户可写。"""
    if shutil.which("setfacl") is None:
        pytest.skip("当前环境没有 setfacl，无法构造默认 ACL")
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    default_acl = subprocess.run(
        ("setfacl", "-d", "-m", "u::rwx,g::rwx,o::rwx", str(anchor)),
        capture_output=True,
        text=True,
        check=False,
    )
    if default_acl.returncode != 0:
        pytest.skip(f"当前文件系统不支持默认 ACL：{default_acl.stderr.strip()}")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts)
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text("# 无运行依赖。\n", encoding="utf-8")
    prefix = anchor / "installation"
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )

    completed = _run_installer(
        tmp_path=tmp_path,
        wheel=wheel,
        wheelhouse=wheelhouse,
        runtime_lock=runtime_lock,
        prefix=prefix,
        fake_bin=fake_bin,
        os_release=os_release,
    )

    assert completed.returncode == 0, completed.stderr
    version_directory = prefix / "versions" / APPLICATION_VERSION
    relaxed = sorted(
        str(path.relative_to(version_directory))
        for path in version_directory.rglob("*")
        if not path.is_symlink() and path.lstat().st_mode & 0o022
    )
    assert relaxed == []


def test_installed_system_package_must_equal_the_snapshot_candidate(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts)
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text("# 无运行依赖。\n", encoding="utf-8")
    prefix = tmp_path / "installation"
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )

    completed = _run_installer(
        tmp_path=tmp_path,
        wheel=wheel,
        wheelhouse=wheelhouse,
        runtime_lock=runtime_lock,
        prefix=prefix,
        fake_bin=fake_bin,
        os_release=os_release,
        extra_environment={"FAKE_DPKG_DRIFT_PACKAGE": "ffmpeg"},
    )

    assert completed.returncode != 0
    assert "snapshot 候选版本" in completed.stderr
    assert not prefix.exists() or not (prefix / "current").exists()


def test_system_package_drift_during_readiness_prevents_ready_marker(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts)
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text("# 无运行依赖。\n", encoding="utf-8")
    prefix = tmp_path / "installation"
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )

    completed = _run_installer(
        tmp_path=tmp_path,
        wheel=wheel,
        wheelhouse=wheelhouse,
        runtime_lock=runtime_lock,
        prefix=prefix,
        fake_bin=fake_bin,
        os_release=os_release,
        extra_environment={"FAKE_DPKG_DELAYED_DRIFT_PACKAGE": "ffmpeg"},
    )

    assert completed.returncode != 0
    assert "snapshot 候选版本" in completed.stderr
    assert not (prefix / "current").exists()
    assert not (prefix / "versions" / APPLICATION_VERSION).exists()


def test_large_ffmpeg_capability_listing_does_not_trigger_pipefail(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts)
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text("# 无运行依赖。\n", encoding="utf-8")
    prefix = tmp_path / "installation"
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )

    completed = _run_installer(
        tmp_path=tmp_path,
        wheel=wheel,
        wheelhouse=wheelhouse,
        runtime_lock=runtime_lock,
        prefix=prefix,
        fake_bin=fake_bin,
        os_release=os_release,
        extra_environment={"FAKE_LARGE_FILTER_LISTING": "1"},
    )

    assert completed.returncode == 0, completed.stderr
    assert (prefix / "current" / "READY").is_file()


def test_reinstalling_the_same_locked_candidate_is_idempotent(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts)
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text(
        "# 当前生产运行时没有第三方 Python 依赖。\n",
        encoding="utf-8",
    )
    prefix = tmp_path / "installation"
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )
    command = (
        str(INSTALLER),
        "--wheel",
        str(wheel),
        "--wheel-sha256",
        _sha256(wheel),
        "--wheelhouse",
        str(wheelhouse),
        "--runtime-lock",
        str(runtime_lock),
        "--runtime-lock-sha256",
        _sha256(runtime_lock),
        "--apt-snapshot-id",
        SNAPSHOT_ID,
        "--prefix",
        str(prefix),
    )
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "INSTALL_PRODUCTION_TEST_OS_RELEASE_FILE": str(os_release),
    }

    first = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    version_directory = prefix / "versions" / APPLICATION_VERSION
    manifest_path = version_directory / "installation-manifest.json"
    original_manifest = manifest_path.read_bytes()
    original_ready = (version_directory / "READY").read_bytes()
    original_manifest_mtime = manifest_path.stat().st_mtime_ns
    original_ready_mtime = (version_directory / "READY").stat().st_mtime_ns
    original_venv_inode = (version_directory / "venv").stat().st_ino
    operator_marker = version_directory / "operator-marker"
    operator_marker.write_text("keep\n", encoding="utf-8")
    first_ffmpeg_probe_count = sum(
        line == "ffmpeg\t-version"
        for line in trace.read_text(encoding="utf-8").splitlines()
    )

    second = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert second.returncode == 0, second.stderr
    assert manifest_path.read_bytes() == original_manifest
    assert (version_directory / "READY").read_bytes() == original_ready
    assert manifest_path.stat().st_mtime_ns == original_manifest_mtime
    assert (version_directory / "READY").stat().st_mtime_ns == (
        original_ready_mtime
    )
    assert (version_directory / "venv").stat().st_ino == original_venv_inode
    assert operator_marker.read_text(encoding="utf-8") == "keep\n"
    assert (prefix / "current").readlink() == Path(
        f"versions/{APPLICATION_VERSION}"
    )
    second_ffmpeg_probe_count = sum(
        line == "ffmpeg\t-version"
        for line in trace.read_text(encoding="utf-8").splitlines()
    )
    assert second_ffmpeg_probe_count == first_ffmpeg_probe_count + 1


def test_reinstall_recovers_an_unreferenced_unready_version(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts)
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text("# 无运行依赖。\n", encoding="utf-8")
    prefix = tmp_path / "installation"
    partial_version = prefix / "versions" / APPLICATION_VERSION
    partial_version.mkdir(parents=True)
    (partial_version / "interrupted-install").write_text(
        "not-ready\n",
        encoding="utf-8",
    )
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )

    completed = subprocess.run(
        (
            str(INSTALLER),
            "--wheel",
            str(wheel),
            "--wheel-sha256",
            _sha256(wheel),
            "--wheelhouse",
            str(wheelhouse),
            "--runtime-lock",
            str(runtime_lock),
            "--runtime-lock-sha256",
            _sha256(runtime_lock),
            "--apt-snapshot-id",
            SNAPSHOT_ID,
            "--prefix",
            str(prefix),
        ),
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "INSTALL_PRODUCTION_TEST_OS_RELEASE_FILE": str(os_release),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert not (partial_version / "interrupted-install").exists()
    assert (partial_version / "READY").is_file()
    assert (prefix / "current").readlink() == Path(
        f"versions/{APPLICATION_VERSION}"
    )


def test_failed_readiness_restores_the_previous_version_and_cleans_candidate(
    tmp_path,
):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    first_wheel = _write_candidate_wheel(artifacts)
    second_version = "4.8.0"
    second_wheel = _write_candidate_wheel(
        artifacts,
        version=second_version,
    )
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text(
        "# 当前生产运行时没有第三方 Python 依赖。\n",
        encoding="utf-8",
    )
    prefix = tmp_path / "installation"
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "INSTALL_PRODUCTION_TEST_OS_RELEASE_FILE": str(os_release),
    }

    def install(wheel: Path):
        return subprocess.run(
            (
                str(INSTALLER),
                "--wheel",
                str(wheel),
                "--wheel-sha256",
                _sha256(wheel),
                "--wheelhouse",
                str(wheelhouse),
                "--runtime-lock",
                str(runtime_lock),
                "--runtime-lock-sha256",
                _sha256(runtime_lock),
                "--apt-snapshot-id",
                SNAPSHOT_ID,
                "--prefix",
                str(prefix),
            ),
            cwd=tmp_path,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    first = install(first_wheel)
    assert first.returncode == 0, first.stderr
    first_directory = prefix / "versions" / APPLICATION_VERSION
    first_manifest = (first_directory / "installation-manifest.json").read_bytes()

    _write_executable(
        fake_bin / "ffmpeg",
        "#!/bin/sh\n"
        f"printf 'ffmpeg' >> {trace!s}\n"
        f"printf '\\t%s' \"$@\" >> {trace!s}\n"
        f"printf '\\n' >> {trace!s}\n"
        "case \" $* \" in\n"
        "  *' -version '*) printf 'ffmpeg version 6.1.1-3ubuntu5\\n';;\n"
        "  *' -filters '*) printf ' T.. subtitles V->V Render subtitles\\n';;\n"
        "  *' -encoders '*) printf ' V....D libx264\\n A..... aac\\n';;\n"
        "  *) exit 9;;\n"
        "esac\n",
    )

    second = install(second_wheel)

    assert second.returncode != 0
    assert (prefix / "current").readlink() == Path(
        f"versions/{APPLICATION_VERSION}"
    )
    assert (first_directory / "installation-manifest.json").read_bytes() == (
        first_manifest
    )
    assert (first_directory / "READY").is_file()
    assert not (prefix / "versions" / second_version).exists()
    assert not any(
        child.name.startswith((".current", ".readiness"))
        for child in prefix.iterdir()
    )


def test_next_run_recovers_a_sigkill_during_post_switch_readiness(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    first_wheel = _write_candidate_wheel(artifacts)
    second_version = "4.8.0"
    second_wheel = _write_candidate_wheel(
        artifacts,
        version=second_version,
    )
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text("# 无运行依赖。\n", encoding="utf-8")
    prefix = tmp_path / "installation"
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )

    first = _run_installer(
        tmp_path=tmp_path,
        wheel=first_wheel,
        wheelhouse=wheelhouse,
        runtime_lock=runtime_lock,
        prefix=prefix,
        fake_bin=fake_bin,
        os_release=os_release,
    )
    assert first.returncode == 0, first.stderr

    kill_marker = tmp_path / "readiness-killed"
    _write_executable(
        fake_bin / "fc-list",
        "#!/bin/sh\n"
        f": > {shlex.quote(str(kill_marker))}\n"
        "kill -KILL \"$PPID\"\n"
        "exit 137\n",
    )
    interrupted = _run_installer(
        tmp_path=tmp_path,
        wheel=second_wheel,
        wheelhouse=wheelhouse,
        runtime_lock=runtime_lock,
        prefix=prefix,
        fake_bin=fake_bin,
        os_release=os_release,
    )

    assert interrupted.returncode < 0
    assert kill_marker.is_file()
    assert (prefix / "current").readlink() == Path(
        f"versions/{second_version}"
    )
    assert not (prefix / "versions" / second_version / "READY").exists()
    assert (prefix / ".install-transaction").is_dir()

    recovery = _run_installer(
        tmp_path=tmp_path,
        wheel=second_wheel,
        wheel_sha256="0" * 64,
        wheelhouse=wheelhouse,
        runtime_lock=runtime_lock,
        prefix=prefix,
        fake_bin=fake_bin,
        os_release=os_release,
    )

    assert recovery.returncode != 0
    assert "wheel SHA-256" in recovery.stderr
    assert (prefix / "current").readlink() == Path(
        f"versions/{APPLICATION_VERSION}"
    )
    assert (prefix / "versions" / APPLICATION_VERSION / "READY").is_file()
    assert not (prefix / "versions" / second_version).exists()
    assert not (prefix / ".install-transaction").exists()


def test_strict_readiness_reports_all_detected_environment_issues(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts)
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text("# 无运行依赖。\n", encoding="utf-8")
    prefix = tmp_path / "installation"
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    _write_executable(
        fake_bin / "ffmpeg",
        "#!/bin/sh\n"
        "case \" $* \" in\n"
        "  *' -version '*) printf 'ffmpeg version 6.1.1\\n';;\n"
        "  *' -filters '*) printf '\\n';;\n"
        "  *' -encoders '*) printf '\\n';;\n"
        "  *) exit 9;;\n"
        "esac\n",
    )
    _write_executable(
        fake_bin / "ffprobe",
        "#!/bin/sh\n"
        "case \" $* \" in\n"
        "  *' -version '*) printf 'ffprobe version 6.2.0\\n';;\n"
        "  *) exit 9;;\n"
        "esac\n",
    )
    _write_executable(fake_bin / "fc-list", "#!/bin/sh\nexit 1\n")
    _write_executable(
        fake_bin / "fc-match",
        "#!/bin/sh\nprintf 'DejaVu Sans\\n/missing-font.ttf\\n'\n",
    )
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )

    completed = subprocess.run(
        (
            str(INSTALLER),
            "--wheel",
            str(wheel),
            "--wheel-sha256",
            _sha256(wheel),
            "--wheelhouse",
            str(wheelhouse),
            "--runtime-lock",
            str(runtime_lock),
            "--runtime-lock-sha256",
            _sha256(runtime_lock),
            "--apt-snapshot-id",
            SNAPSHOT_ID,
            "--prefix",
            str(prefix),
        ),
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "INSTALL_PRODUCTION_TEST_OS_RELEASE_FILE": str(os_release),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "FFmpeg 与 ffprobe 上游版本不一致" in completed.stderr
    assert "FFmpeg 缺少 subtitles 滤镜" in completed.stderr
    assert "FFmpeg 缺少 libx264 编码器" in completed.stderr
    assert "FFmpeg 缺少 AAC 编码器" in completed.stderr
    assert "没有找到认证中文字体" in completed.stderr
    assert not (prefix / "current").exists()
    assert not (prefix / "versions" / APPLICATION_VERSION).exists()


def test_new_version_does_not_overwrite_old_or_accept_same_version_rebuild(
    tmp_path,
):
    first_artifacts = tmp_path / "first-artifacts"
    first_artifacts.mkdir()
    first_wheel = _write_candidate_wheel(first_artifacts)
    rebuilt_artifacts = tmp_path / "rebuilt-artifacts"
    rebuilt_artifacts.mkdir()
    rebuilt_wheel = _write_candidate_wheel(rebuilt_artifacts)
    with ZipFile(rebuilt_wheel, "a", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            "video_auto_editor/build_identity.py",
            "BUILD_IDENTITY = 'different-candidate'\n",
        )
    next_artifacts = tmp_path / "next-artifacts"
    next_artifacts.mkdir()
    next_version = "4.8.0"
    next_wheel = _write_candidate_wheel(
        next_artifacts,
        version=next_version,
    )
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = tmp_path / "requirements-runtime.lock"
    runtime_lock.write_text("# 无运行依赖。\n", encoding="utf-8")
    prefix = tmp_path / "installation"
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "INSTALL_PRODUCTION_TEST_OS_RELEASE_FILE": str(os_release),
    }

    def install(wheel: Path):
        return subprocess.run(
            (
                str(INSTALLER),
                "--wheel",
                str(wheel),
                "--wheel-sha256",
                _sha256(wheel),
                "--wheelhouse",
                str(wheelhouse),
                "--runtime-lock",
                str(runtime_lock),
                "--runtime-lock-sha256",
                _sha256(runtime_lock),
                "--apt-snapshot-id",
                SNAPSHOT_ID,
                "--prefix",
                str(prefix),
            ),
            cwd=tmp_path,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    first = install(first_wheel)
    assert first.returncode == 0, first.stderr
    first_directory = prefix / "versions" / APPLICATION_VERSION
    first_manifest = (first_directory / "installation-manifest.json").read_bytes()

    rebuilt = install(rebuilt_wheel)
    assert rebuilt.returncode != 0
    assert "同版本" in rebuilt.stderr
    assert (prefix / "current").readlink() == Path(
        f"versions/{APPLICATION_VERSION}"
    )
    assert (first_directory / "installation-manifest.json").read_bytes() == (
        first_manifest
    )

    upgraded = install(next_wheel)
    assert upgraded.returncode == 0, upgraded.stderr
    assert (prefix / "current").readlink() == Path(
        f"versions/{next_version}"
    )
    assert (first_directory / "READY").is_file()
    assert (prefix / "versions" / next_version / "READY").is_file()


def test_invalid_snapshot_timestamp_is_rejected_before_apt(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts)
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text("# 无运行依赖。\n", encoding="utf-8")
    prefix = tmp_path / "installation"
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )

    completed = subprocess.run(
        (
            str(INSTALLER),
            "--wheel",
            str(wheel),
            "--wheel-sha256",
            _sha256(wheel),
            "--wheelhouse",
            str(wheelhouse),
            "--runtime-lock",
            str(runtime_lock),
            "--runtime-lock-sha256",
            _sha256(runtime_lock),
            "--apt-snapshot-id",
            "20261340T250000Z",
            "--prefix",
            str(prefix),
        ),
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "INSTALL_PRODUCTION_TEST_OS_RELEASE_FILE": str(os_release),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "snapshot" in completed.stderr
    assert not trace.exists()
    assert not prefix.exists()


def test_wheelhouse_rejects_artifacts_resolved_from_outside(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts)
    outside = tmp_path / "outside"
    outside.mkdir()
    dependency = _write_runtime_dependency_wheel(outside)
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    (wheelhouse / dependency.name).symlink_to(dependency)
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text(
        "sample-dependency==1.0 "
        f"--hash=sha256:{_sha256(dependency)}\n",
        encoding="utf-8",
    )
    prefix = tmp_path / "installation"
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )

    completed = subprocess.run(
        (
            str(INSTALLER),
            "--wheel",
            str(wheel),
            "--wheel-sha256",
            _sha256(wheel),
            "--wheelhouse",
            str(wheelhouse),
            "--runtime-lock",
            str(runtime_lock),
            "--runtime-lock-sha256",
            _sha256(runtime_lock),
            "--apt-snapshot-id",
            SNAPSHOT_ID,
            "--prefix",
            str(prefix),
        ),
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "INSTALL_PRODUCTION_TEST_OS_RELEASE_FILE": str(os_release),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "wheelhouse" in completed.stderr
    assert not trace.exists()
    assert not prefix.exists()


def test_managed_prefix_rejects_a_symlink_lock_file_without_truncating_target(
    tmp_path,
):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts)
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text("# 无运行依赖。\n", encoding="utf-8")
    prefix = tmp_path / "installation"
    prefix.mkdir()
    sentinel = tmp_path / "operator-data"
    sentinel.write_text("must-survive\n", encoding="utf-8")
    (prefix / ".install.lock").symlink_to(sentinel)
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )

    completed = _run_installer(
        tmp_path=tmp_path,
        wheel=wheel,
        wheelhouse=wheelhouse,
        runtime_lock=runtime_lock,
        prefix=prefix,
        fake_bin=fake_bin,
        os_release=os_release,
    )

    assert completed.returncode != 0
    assert "符号链接" in completed.stderr
    assert sentinel.read_text(encoding="utf-8") == "must-survive\n"


def test_managed_versions_directory_cannot_be_a_symlink(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts)
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text("# 无运行依赖。\n", encoding="utf-8")
    prefix = tmp_path / "installation"
    prefix.mkdir()
    outside = tmp_path / "outside"
    candidate_outside = outside / APPLICATION_VERSION
    candidate_outside.mkdir(parents=True)
    sentinel = candidate_outside / "operator-data"
    sentinel.write_text("must-survive\n", encoding="utf-8")
    (prefix / "versions").symlink_to(outside, target_is_directory=True)
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )

    completed = _run_installer(
        tmp_path=tmp_path,
        wheel=wheel,
        wheelhouse=wheelhouse,
        runtime_lock=runtime_lock,
        prefix=prefix,
        fake_bin=fake_bin,
        os_release=os_release,
    )

    assert completed.returncode != 0
    assert "versions" in completed.stderr
    assert sentinel.read_text(encoding="utf-8") == "must-survive\n"


def test_managed_prefix_cannot_be_group_or_world_writable(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts)
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text("# 无运行依赖。\n", encoding="utf-8")
    prefix = tmp_path / "installation"
    prefix.mkdir(mode=0o777)
    prefix.chmod(0o777)
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )

    completed = _run_installer(
        tmp_path=tmp_path,
        wheel=wheel,
        wheelhouse=wheelhouse,
        runtime_lock=runtime_lock,
        prefix=prefix,
        fake_bin=fake_bin,
        os_release=os_release,
    )

    assert completed.returncode != 0
    assert "不得允许组或其他用户写入" in completed.stderr
    assert not trace.exists()


def test_managed_prefix_rejects_an_untrusted_writable_parent(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts)
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text("# 无运行依赖。\n", encoding="utf-8")
    writable_parent = tmp_path / "writable-parent"
    writable_parent.mkdir(mode=0o777)
    writable_parent.chmod(0o777)
    prefix = writable_parent / "installation"
    prefix.mkdir()
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )

    completed = _run_installer(
        tmp_path=tmp_path,
        wheel=wheel,
        wheelhouse=wheelhouse,
        runtime_lock=runtime_lock,
        prefix=prefix,
        fake_bin=fake_bin,
        os_release=os_release,
    )

    assert completed.returncode != 0
    assert "安装前缀目录链" in completed.stderr
    assert "不得允许组或其他用户写入" in completed.stderr
    assert not trace.exists()


def test_missing_prefix_rejects_a_shared_writable_creation_anchor(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts)
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text("# 无运行依赖。\n", encoding="utf-8")
    prefix_identity = hashlib.sha256(
        os.fsencode(str(tmp_path)),
    ).hexdigest()[:16]
    prefix = Path("/tmp") / f"video-auto-editor-install-test-{prefix_identity}"
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )

    try:
        completed = _run_installer(
            tmp_path=tmp_path,
            wheel=wheel,
            wheelhouse=wheelhouse,
            runtime_lock=runtime_lock,
            prefix=prefix,
            fake_bin=fake_bin,
            os_release=os_release,
        )

        assert completed.returncode != 0
        assert "安装前缀创建锚点" in completed.stderr
        assert not prefix.exists()
    finally:
        if prefix.is_dir() and not prefix.is_symlink():
            shutil.rmtree(prefix)


def test_trusted_temporary_root_requires_sticky_shared_write_protection(
    tmp_path,
):
    temporary_root = tmp_path / "unsafe-temporary-root"
    temporary_root.mkdir(mode=0o777)
    temporary_root.chmod(0o777)

    completed = subprocess.run(
        (
            "/bin/bash",
            "-c",
            'source "$1"; validate_trusted_temporary_root "$2" "$3"',
            "temporary-root-test",
            str(PRODUCTION_INSTALLER),
            str(temporary_root),
            str(os.geteuid()),
        ),
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "共享写临时目录根必须启用 sticky 保护" in completed.stderr


def test_installer_ignores_an_inherited_temporary_directory(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts)
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text("# 无运行依赖。\n", encoding="utf-8")
    prefix = tmp_path / "installation"
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )

    completed = _run_installer(
        tmp_path=tmp_path,
        wheel=wheel,
        wheelhouse=wheelhouse,
        runtime_lock=runtime_lock,
        prefix=prefix,
        fake_bin=fake_bin,
        os_release=os_release,
        extra_environment={
            "TMPDIR": str(tmp_path / "attacker-controlled" / "missing"),
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert (prefix / "versions" / APPLICATION_VERSION / "READY").is_file()


def test_hashed_runtime_dependency_installs_only_from_local_wheelhouse(
    tmp_path,
):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts)
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    dependency = _write_runtime_dependency_wheel(wheelhouse)
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text(
        "sample-dependency==1.0 "
        f"--hash=sha256:{_sha256(dependency)}\n",
        encoding="utf-8",
    )
    prefix = tmp_path / "installation"
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )

    completed = subprocess.run(
        (
            str(INSTALLER),
            "--wheel",
            str(wheel),
            "--wheel-sha256",
            _sha256(wheel),
            "--wheelhouse",
            str(wheelhouse),
            "--runtime-lock",
            str(runtime_lock),
            "--runtime-lock-sha256",
            _sha256(runtime_lock),
            "--apt-snapshot-id",
            SNAPSHOT_ID,
            "--prefix",
            str(prefix),
        ),
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "INSTALL_PRODUCTION_TEST_OS_RELEASE_FILE": str(os_release),
            "PIP_INDEX_URL": "https://network-must-not-be-used.invalid/simple",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    imported = subprocess.run(
        (
            str(prefix / "current" / "venv" / "bin" / "python"),
            "-c",
            "import sample_dependency; print(sample_dependency.VALUE)",
        ),
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=True,
    )
    assert imported.stdout == "locked-wheelhouse\n"
    manifest = json.loads(
        (
            prefix
            / "versions"
            / APPLICATION_VERSION
            / "installation-manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["wheelhouse"] == [
        {"filename": dependency.name, "sha256": _sha256(dependency)}
    ]


def test_verified_inputs_are_installed_from_a_private_immutable_snapshot(
    tmp_path,
):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts, payload="locked-candidate")
    replacement_directory = tmp_path / "replacement"
    replacement_directory.mkdir()
    replacement = _write_candidate_wheel(
        replacement_directory,
        payload="mutated-after-verification",
    )
    expected_wheel_sha256 = _sha256(wheel)
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text("# 无运行依赖。\n", encoding="utf-8")
    prefix = tmp_path / "installation"
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    real_python = shutil.which("python3.12")
    assert real_python is not None
    mutation_marker = tmp_path / "input-mutated"
    _write_executable(
        fake_bin / "python3.12",
        "#!/bin/sh\n"
        "if [ \"${1-}\" = -I ] && [ \"${2-}\" = - ] "
        "&& [ -n \"${3-}\" ] "
        f"&& [ ! -e {shlex.quote(str(mutation_marker))} ]; then\n"
        f"  : > {shlex.quote(str(mutation_marker))}\n"
        f"  /bin/cp -- {shlex.quote(str(replacement))} "
        f"{shlex.quote(str(wheel))}\n"
        "fi\n"
        f"exec {shlex.quote(real_python)} \"$@\"\n",
    )
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )

    completed = _run_installer(
        tmp_path=tmp_path,
        wheel=wheel,
        wheel_sha256=expected_wheel_sha256,
        wheelhouse=wheelhouse,
        runtime_lock=runtime_lock,
        prefix=prefix,
        fake_bin=fake_bin,
        os_release=os_release,
    )

    assert completed.returncode == 0, completed.stderr
    installed_payload = subprocess.run(
        (
            str(prefix / "current" / "venv" / "bin" / "python"),
            "-c",
            "from video_auto_editor import PAYLOAD; print(PAYLOAD)",
        ),
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=True,
    )
    assert mutation_marker.is_file()
    assert installed_payload.stdout == "locked-candidate\n"
    manifest = json.loads(
        (prefix / "current" / "installation-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["application"]["wheel"]["sha256"] == (
        expected_wheel_sha256
    )


def test_runtime_lock_must_form_a_complete_dependency_closure(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts)
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    dependency = _write_runtime_dependency_wheel(
        wheelhouse,
        requires_dist="missing-transitive-dependency==9.9",
    )
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text(
        "sample-dependency==1.0 "
        f"--hash=sha256:{_sha256(dependency)}\n",
        encoding="utf-8",
    )
    prefix = tmp_path / "installation"
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )

    completed = _run_installer(
        tmp_path=tmp_path,
        wheel=wheel,
        wheelhouse=wheelhouse,
        runtime_lock=runtime_lock,
        prefix=prefix,
        fake_bin=fake_bin,
        os_release=os_release,
    )

    assert completed.returncode != 0
    assert "missing-transitive-dependency" in (completed.stdout + completed.stderr)
    assert not (prefix / "current").exists()
    assert not (prefix / "versions" / APPLICATION_VERSION).exists()


@pytest.mark.parametrize(
    ("os_release_contents", "architecture"),
    [
        ('ID=debian\nVERSION_ID="12"\n', "amd64"),
        ('ID=ubuntu\nVERSION_ID="24.04"\n', "arm64"),
    ],
)
def test_uncertified_platform_is_rejected_before_apt(
    tmp_path,
    os_release_contents,
    architecture,
):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts)
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text("# 无运行依赖。\n", encoding="utf-8")
    prefix = tmp_path / "installation"
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    _write_executable(
        fake_bin / "dpkg",
        "#!/bin/sh\n"
        "if [ \"${1-}\" = \"--print-architecture\" ]; then "
        f"printf '{architecture}\\n'; exit 0; fi\n"
        "exit 64\n",
    )
    os_release = tmp_path / "os-release"
    os_release.write_text(os_release_contents, encoding="utf-8")

    completed = subprocess.run(
        (
            str(INSTALLER),
            "--wheel",
            str(wheel),
            "--wheel-sha256",
            _sha256(wheel),
            "--wheelhouse",
            str(wheelhouse),
            "--runtime-lock",
            str(runtime_lock),
            "--runtime-lock-sha256",
            _sha256(runtime_lock),
            "--apt-snapshot-id",
            SNAPSHOT_ID,
            "--prefix",
            str(prefix),
        ),
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "INSTALL_PRODUCTION_TEST_OS_RELEASE_FILE": str(os_release),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert not trace.exists()
    assert not prefix.exists()


def test_uncertified_cpython_is_rejected_after_snapshot_packages(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts)
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text("# 无运行依赖。\n", encoding="utf-8")
    prefix = tmp_path / "installation"
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    _write_executable(
        fake_bin / "python3.12",
        "#!/bin/sh\n"
        f"printf 'python3.12\\tunsupported\\n' >> {trace!s}\n"
        "printf '生产安装要求 CPython >=3.12.3,<3.13\\n' >&2\n"
        "exit 1\n",
    )
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )

    completed = subprocess.run(
        (
            str(INSTALLER),
            "--wheel",
            str(wheel),
            "--wheel-sha256",
            _sha256(wheel),
            "--wheelhouse",
            str(wheelhouse),
            "--runtime-lock",
            str(runtime_lock),
            "--runtime-lock-sha256",
            _sha256(runtime_lock),
            "--apt-snapshot-id",
            SNAPSHOT_ID,
            "--prefix",
            str(prefix),
        ),
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "INSTALL_PRODUCTION_TEST_OS_RELEASE_FILE": str(os_release),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    commands = trace.read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith("apt-get\t") for line in commands) == 2
    assert commands[-1] == "python3.12\tunsupported"
    assert not prefix.exists()


@pytest.mark.parametrize("artifact_kind", ["wheel", "runtime-lock"])
def test_changed_locked_artifact_is_rejected_before_version_creation(
    tmp_path,
    artifact_kind,
):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts)
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text("# 无运行依赖。\n", encoding="utf-8")
    expected_wheel_hash = _sha256(wheel)
    expected_lock_hash = _sha256(runtime_lock)
    if artifact_kind == "wheel":
        expected_wheel_hash = "0" * 64
    else:
        expected_lock_hash = "0" * 64
    prefix = tmp_path / "installation"
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )

    completed = subprocess.run(
        (
            str(INSTALLER),
            "--wheel",
            str(wheel),
            "--wheel-sha256",
            expected_wheel_hash,
            "--wheelhouse",
            str(wheelhouse),
            "--runtime-lock",
            str(runtime_lock),
            "--runtime-lock-sha256",
            expected_lock_hash,
            "--apt-snapshot-id",
            SNAPSHOT_ID,
            "--prefix",
            str(prefix),
        ),
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "INSTALL_PRODUCTION_TEST_OS_RELEASE_FILE": str(os_release),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "SHA-256" in completed.stderr
    assert sum(
        line.startswith("apt-get\t")
        for line in trace.read_text(encoding="utf-8").splitlines()
    ) == 2
    assert not prefix.exists()


@pytest.mark.parametrize(
    "runtime_requirement",
    [
        "sample-dependency==1.0",
        "sample-dependency @ file:///tmp/sample_dependency.whl",
        "-e /tmp/sample-dependency",
    ],
)
def test_runtime_lock_rejects_unhashed_or_direct_installation_forms(
    tmp_path,
    runtime_requirement,
):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts)
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text(runtime_requirement + "\n", encoding="utf-8")
    prefix = tmp_path / "installation"
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )

    completed = subprocess.run(
        (
            str(INSTALLER),
            "--wheel",
            str(wheel),
            "--wheel-sha256",
            _sha256(wheel),
            "--wheelhouse",
            str(wheelhouse),
            "--runtime-lock",
            str(runtime_lock),
            "--runtime-lock-sha256",
            _sha256(runtime_lock),
            "--apt-snapshot-id",
            SNAPSHOT_ID,
            "--prefix",
            str(prefix),
        ),
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "INSTALL_PRODUCTION_TEST_OS_RELEASE_FILE": str(os_release),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "精确固定且带 SHA-256" in completed.stderr
    assert not prefix.exists()


def test_application_artifact_must_be_a_prebuilt_wheel(tmp_path):
    source_archive = tmp_path / "video-auto-editor-4.7.0.tar.gz"
    source_archive.write_bytes(b"source distribution")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = tmp_path / "requirements-runtime.lock"
    runtime_lock.write_text("# 无运行依赖。\n", encoding="utf-8")
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)

    completed = subprocess.run(
        (
            str(INSTALLER),
            "--wheel",
            str(source_archive),
            "--wheel-sha256",
            _sha256(source_archive),
            "--wheelhouse",
            str(wheelhouse),
            "--runtime-lock",
            str(runtime_lock),
            "--runtime-lock-sha256",
            _sha256(runtime_lock),
            "--apt-snapshot-id",
            SNAPSHOT_ID,
            "--prefix",
            str(tmp_path / "installation"),
        ),
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "wheel" in completed.stderr
    assert not trace.exists()


def test_wheel_version_cannot_escape_the_managed_versions_directory(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _write_candidate_wheel(artifacts, version="..")
    wheelhouse = artifacts / "wheelhouse"
    wheelhouse.mkdir()
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text("# 无运行依赖。\n", encoding="utf-8")
    prefix = tmp_path / "installation"
    prefix.mkdir()
    sentinel = prefix / "operator-data"
    sentinel.write_text("must-survive\n", encoding="utf-8")
    trace = tmp_path / "commands.trace"
    fake_bin = _write_fake_system_commands(tmp_path, trace)
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\n',
        encoding="utf-8",
    )

    completed = subprocess.run(
        (
            str(INSTALLER),
            "--wheel",
            str(wheel),
            "--wheel-sha256",
            _sha256(wheel),
            "--wheelhouse",
            str(wheelhouse),
            "--runtime-lock",
            str(runtime_lock),
            "--runtime-lock-sha256",
            _sha256(runtime_lock),
            "--apt-snapshot-id",
            SNAPSHOT_ID,
            "--prefix",
            str(prefix),
        ),
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "INSTALL_PRODUCTION_TEST_OS_RELEASE_FILE": str(os_release),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "版本" in completed.stderr
    assert sentinel.read_text(encoding="utf-8") == "must-survive\n"
    assert sum(
        line.startswith("apt-get\t")
        for line in trace.read_text(encoding="utf-8").splitlines()
    ) == 2
