import shutil
import tomllib
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.validate_architecture import (
    APPROVED_PACKAGE_FILES,
    ArchitectureViolation,
    validate_source_tree,
    validate_wheel,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "video_auto_editor"


def _write_approved_wheel(
    tmp_path,
    *,
    source_overrides=None,
    include_entry_points=True,
    entry_point_name="video-auto-editor",
    requires_python="<3.13,>=3.12.3",
):
    wheel = tmp_path / "video_auto_editor-4.7.0-py3-none-any.whl"
    dist_info = "video_auto_editor-4.7.0.dist-info"
    source_overrides = source_overrides or {}
    with ZipFile(wheel, "w") as archive:
        for package_file in APPROVED_PACKAGE_FILES:
            archive.writestr(
                package_file,
                source_overrides.get(package_file, ""),
            )
        archive.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.4\n"
            "Name: video-auto-editor\n"
            "Version: 4.7.0\n"
            f"Requires-Python: {requires_python}\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
        if include_entry_points:
            archive.writestr(
                f"{dist_info}/entry_points.txt",
                "[console_scripts]\n"
                f"{entry_point_name} = video_auto_editor.cli:main\n",
            )
        archive.writestr(f"{dist_info}/RECORD", "")
    return wheel


def test_current_source_tree_matches_the_approved_release_structure():
    validate_source_tree(PROJECT_ROOT)


def test_structure_gate_rejects_a_reverse_package_dependency(tmp_path):
    shutil.copytree(PACKAGE_ROOT, tmp_path / "video_auto_editor")
    runtime_errors = (
        tmp_path / "video_auto_editor" / "runtime" / "errors.py"
    )
    runtime_errors.write_text(
        runtime_errors.read_text(encoding="utf-8")
        + "\nfrom video_auto_editor.delivery.build import DeliveryBuild\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ArchitectureViolation,
        match="runtime.*delivery",
    ):
        validate_source_tree(tmp_path)


def test_structure_gate_rejects_an_adapter_outside_composition_root(
    tmp_path,
):
    shutil.copytree(PACKAGE_ROOT, tmp_path / "video_auto_editor")
    live_application = (
        tmp_path / "video_auto_editor" / "application" / "live.py"
    )
    live_application.write_text(
        live_application.read_text(encoding="utf-8")
        + "\nfrom video_auto_editor.text_model.stepfun import "
        "StepFunTextModel\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ArchitectureViolation,
        match="Adapter.*application",
    ):
        validate_source_tree(tmp_path)


def test_structure_gate_resolves_a_submodule_imported_from_its_package(
    tmp_path,
):
    shutil.copytree(PACKAGE_ROOT, tmp_path / "video_auto_editor")
    live_application = (
        tmp_path / "video_auto_editor" / "application" / "live.py"
    )
    live_application.write_text(
        live_application.read_text(encoding="utf-8")
        + "\nfrom video_auto_editor.text_model import stepfun\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ArchitectureViolation,
        match="Adapter.*application",
    ):
        validate_source_tree(tmp_path)


def test_structure_gate_rejects_a_cross_package_private_import(tmp_path):
    shutil.copytree(PACKAGE_ROOT, tmp_path / "video_auto_editor")
    live_application = (
        tmp_path / "video_auto_editor" / "application" / "live.py"
    )
    live_application.write_text(
        live_application.read_text(encoding="utf-8")
        + "\nfrom video_auto_editor.diagnostics._session import _FileLock\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ArchitectureViolation,
        match="私有导入.*application.*diagnostics",
    ):
        validate_source_tree(tmp_path)


def test_wheel_gate_rejects_an_unapproved_production_module(tmp_path):
    wheel = tmp_path / "video_auto_editor-4.7.0-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("video_auto_editor/context.py", "")

    with pytest.raises(
        ArchitectureViolation,
        match="wheel.*video_auto_editor/context.py",
    ):
        validate_wheel(wheel)


def test_wheel_gate_requires_the_installed_console_script(tmp_path):
    wheel = _write_approved_wheel(
        tmp_path,
        include_entry_points=False,
    )

    with pytest.raises(
        ArchitectureViolation,
        match="console script",
    ):
        validate_wheel(wheel)


def test_wheel_gate_accepts_the_approved_release_artifact(tmp_path):
    wheel = _write_approved_wheel(tmp_path)

    validate_wheel(wheel)


def test_wheel_gate_rejects_an_uncertified_python_range(tmp_path):
    wheel = _write_approved_wheel(
        tmp_path,
        requires_python=">=3.10",
    )

    with pytest.raises(
        ArchitectureViolation,
        match="CPython.*>=3.12.3,<3.13",
    ):
        validate_wheel(wheel)


def test_wheel_gate_preserves_console_script_name_case(tmp_path):
    wheel = _write_approved_wheel(
        tmp_path,
        entry_point_name="VIDEO-AUTO-EDITOR",
    )

    with pytest.raises(
        ArchitectureViolation,
        match="console script",
    ):
        validate_wheel(wheel)


def test_wheel_gate_rechecks_adapter_imports_in_built_sources(tmp_path):
    wheel = _write_approved_wheel(
        tmp_path,
        source_overrides={
            "video_auto_editor/application/live.py": (
                "from video_auto_editor.text_model import stepfun\n"
            )
        },
    )

    with pytest.raises(
        ArchitectureViolation,
        match="Adapter.*application",
    ):
        validate_wheel(wheel)


def test_wheel_gate_rechecks_dependency_direction_in_built_sources(tmp_path):
    wheel = _write_approved_wheel(
        tmp_path,
        source_overrides={
            "video_auto_editor/runtime/errors.py": (
                "from video_auto_editor.delivery import build\n"
            )
        },
    )

    with pytest.raises(
        ArchitectureViolation,
        match="runtime.*delivery",
    ):
        validate_wheel(wheel)


def test_wheel_gate_rejects_stale_contents_for_an_approved_module(tmp_path):
    source_root = tmp_path / "source"
    for package_file in APPROVED_PACKAGE_FILES:
        source_file = source_root / package_file
        source_file.parent.mkdir(parents=True, exist_ok=True)
        source_file.write_text("", encoding="utf-8")
    wheel = _write_approved_wheel(
        tmp_path,
        source_overrides={
            "video_auto_editor/__init__.py": (
                "raise RuntimeError('stale build copy')\n"
            )
        },
    )

    with pytest.raises(
        ArchitectureViolation,
        match="内容.*video_auto_editor/__init__.py",
    ):
        validate_wheel(wheel, source_root=source_root)


def test_release_build_routes_through_the_architecture_gate(
    monkeypatch,
    tmp_path,
):
    import build_backend

    events = []

    class FakeSetuptoolsBackend:
        @staticmethod
        def build_wheel(wheel_directory, config_settings=None):
            events.append(("build", Path(wheel_directory)))
            return "video_auto_editor-4.7.0-py3-none-any.whl"

    monkeypatch.setattr(
        build_backend,
        "_setuptools_backend",
        lambda: FakeSetuptoolsBackend,
    )
    monkeypatch.setattr(
        build_backend,
        "validate_source_tree",
        lambda root: events.append(("source", root)),
    )
    monkeypatch.setattr(
        build_backend,
        "validate_wheel",
        lambda wheel, *, source_root: events.append(
            ("wheel", wheel, source_root)
        ),
    )

    wheel_name = build_backend.build_wheel(str(tmp_path))

    assert wheel_name == "video_auto_editor-4.7.0-py3-none-any.whl"
    assert [event[0] for event in events] == ["source", "build", "wheel"]


def test_release_backend_explicitly_rejects_sdist(tmp_path):
    import build_backend

    with pytest.raises(build_backend.UnsupportedOperation, match="wheel"):
        build_backend.build_sdist(str(tmp_path))


def test_pyproject_selects_the_gated_build_backend():
    pyproject = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert pyproject["build-system"]["build-backend"] == "build_backend"
    assert pyproject["build-system"]["backend-path"] == ["."]
