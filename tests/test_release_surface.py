import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

from video_auto_editor import cli

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "video_auto_editor"


def test_release_source_matches_the_approved_top_level_layout():
    approved_entries = {
        "__init__.py",
        "application",
        "cache",
        "cli.py",
        "clip_planning",
        "composition.py",
        "configuration",
        "delivery",
        "diagnostics",
        "runtime",
        "source_analysis",
        "subtitle_optimization",
        "text_model",
        "topic_review",
        "transcription",
        "workspace",
    }
    actual_entries = {
        entry.name
        for entry in PACKAGE_ROOT.iterdir()
        if entry.suffix == ".py"
        or (entry.is_dir() and (entry / "__init__.py").is_file())
    }

    assert actual_entries == approved_entries


def test_result_kind_is_owned_only_by_clip_planning():
    from video_auto_editor import diagnostics, runtime
    from video_auto_editor.clip_planning import ResultKind

    assert ResultKind.CLIPS.value == "clips"
    assert ResultKind.EMPTY.value == "empty"
    assert not hasattr(diagnostics, "ResultKind")
    assert not hasattr(runtime, "ResultKind")
    assert importlib.util.find_spec("video_auto_editor.runtime.result") is None
    assert (
        importlib.util.find_spec(
            "video_auto_editor.runtime._classified_failure"
        )
        is None
    )


def test_cache_facts_are_not_forwarded_by_diagnostics():
    from video_auto_editor import diagnostics
    from video_auto_editor.cache import CacheNamespace, CacheOutcome

    assert CacheNamespace.TRANSCRIPT.value == "transcript"
    assert CacheOutcome.HIT.value == "hit"
    assert not hasattr(diagnostics, "CacheNamespace")
    assert not hasattr(diagnostics, "CacheOutcome")


def test_application_is_the_only_public_owner_of_run_outcomes():
    from video_auto_editor import diagnostics
    from video_auto_editor.application import LiveRunOutcome

    assert LiveRunOutcome.__module__ == "video_auto_editor.application.live"
    assert not hasattr(diagnostics, "RunOutcome")
    assert hasattr(diagnostics, "DiagnosticCompletion")


def test_importing_application_does_not_load_concrete_adapters():
    adapter_modules = {
        "video_auto_editor.cache._filesystem",
        "video_auto_editor.diagnostics.collecting",
        "video_auto_editor.diagnostics.persistent",
        "video_auto_editor.text_model.deterministic",
        "video_auto_editor.text_model.stepfun",
        "video_auto_editor.transcription.deterministic",
        "video_auto_editor.transcription.stepaudio",
    }
    program = (
        "import sys\n"
        "import video_auto_editor.application\n"
        f"targets = {adapter_modules!r}\n"
        "print('\\n'.join(sorted(targets.intersection(sys.modules))))\n"
    )

    completed = subprocess.run(
        (sys.executable, "-c", program),
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout == "\n"


def test_readiness_does_not_publish_a_test_only_system_adapter():
    from video_auto_editor.application import readiness

    assert not hasattr(readiness, "SystemProbe")
    assert readiness.__all__ == ["Readiness"]


def test_release_package_does_not_ship_the_test_composition_root():
    assert not (PACKAGE_ROOT / "application" / "_deterministic.py").exists()


@pytest.mark.parametrize(
    ("public_module", "retired_forwarded_module"),
    [
        (
            "video_auto_editor.delivery.build",
            "video_auto_editor.delivery._build",
        ),
        (
            "video_auto_editor.delivery.verification",
            "video_auto_editor.delivery._verification",
        ),
        (
            "video_auto_editor.delivery.publication",
            "video_auto_editor.delivery._publication",
        ),
        (
            "video_auto_editor.transcription.reconciliation",
            "video_auto_editor.transcription._reconciliation",
        ),
    ],
)
def test_release_uses_the_approved_owner_module_paths(
    public_module,
    retired_forwarded_module,
):
    assert importlib.util.find_spec(public_module) is not None
    assert importlib.util.find_spec(retired_forwarded_module) is None


def test_delivery_package_does_not_forward_owner_contracts():
    from video_auto_editor import delivery

    forwarded_names = {
        "DeliveryBuild",
        "DeliveryVerification",
        "Publication",
        "UnverifiedDelivery",
    }

    assert forwarded_names.isdisjoint(vars(delivery))


def test_delivery_schema_versions_have_one_owner():
    from video_auto_editor.delivery.schema import DELIVERY_SCHEMA_VERSIONS

    assert DELIVERY_SCHEMA_VERSIONS == {
        "manifest": "delivery_manifest.v1",
        "metadata": "short_video_catalog.v1",
        "plan": "clip_plan.v1",
        "transcript": "transcript.v1",
    }
    application_projection = (
        PACKAGE_ROOT / "application" / "run_interpretation.py"
    ).read_text(encoding="utf-8")
    assert '"delivery_manifest.v1"' not in application_projection


@pytest.mark.parametrize(
    "module_name",
    [
        "video_auto_editor.config",
        "video_auto_editor.context",
        "video_auto_editor.dedup",
        "video_auto_editor.export",
        "video_auto_editor.media",
        "video_auto_editor.models",
        "video_auto_editor.plan",
        "video_auto_editor.preflight",
        "video_auto_editor.prototype_transcription_contract",
        "video_auto_editor.prototype_transcription_contract_logic",
        "video_auto_editor.report",
        "video_auto_editor.review",
        "video_auto_editor.scoring",
        "video_auto_editor.selection",
        "video_auto_editor.silence",
        "video_auto_editor.subtitle",
        "video_auto_editor.subtitle_align",
        "video_auto_editor.subtitle_optimizer",
        "video_auto_editor.topic",
        "video_auto_editor.transcript",
    ],
)
def test_release_package_does_not_expose_the_retired_live_stack(module_name):
    assert importlib.util.find_spec(module_name) is None


def test_release_cli_does_not_expose_the_retired_live_orchestration():
    assert not hasattr(cli, "process_live_video")


def test_release_metadata_has_no_runtime_dependencies():
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    project_section = pyproject.split("[project]", 1)[1].split("\n[", 1)[0]

    assert re.search(r"^dependencies\s*=", project_section, re.MULTILINE) is None
    assert "whisper" not in project_section.casefold()
    assert not (PROJECT_ROOT / "requirements.txt").exists()


def test_release_metadata_locks_the_certified_python_and_runtime_inputs():
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    project_section = pyproject.split("[project]", 1)[1].split("\n[", 1)[0]
    requirement = re.search(
        r'^requires-python\s*=\s*"([^"]+)"$',
        project_section,
        re.MULTILINE,
    )

    assert requirement is not None
    assert requirement.group(1) == ">=3.12.3,<3.13"
    runtime_lock = PROJECT_ROOT / "requirements-runtime.lock"
    assert runtime_lock.is_file()
    assert all(
        not line.strip() or line.lstrip().startswith("#")
        for line in runtime_lock.read_text(encoding="utf-8").splitlines()
    )


def test_build_and_gate_dependencies_are_fully_pinned_to_hashed_wheels():
    expected = {
        "build": (
            "1.3.0",
            "7145f0b5061ba90a1500d60bd1b13ca0a8a4cebdd0cc16ed8adf1c0e739f43b4",
        ),
        "iniconfig": (
            "2.3.0",
            "f631c04d2c48c52b84d0d0549c99ff3859c98df65b3101406327ecc7d53fbf12",
        ),
        "packaging": (
            "26.2",
            "5fc45236b9446107ff2415ce77c807cee2862cb6fac22b8a73826d0693b0980e",
        ),
        "pluggy": (
            "1.6.0",
            "e920276dd6813095e9377c0bc5566d94c932c33b27a3e3945d8389c374dd4746",
        ),
        "pygments": (
            "2.20.0",
            "81a9e26dd42fd28a23a2d169d86d7ac03b46e2f8b59ed4698fb4785f946d0176",
        ),
        "pyproject-hooks": (
            "1.2.0",
            "9e5c6bfa8dcc30091c74b0cf803c81fdd29d94f01992a7707bc97babb1141913",
        ),
        "pytest": (
            "9.0.3",
            "2c5efc453d45394fdd706ade797c0a81091eccd1d6e4bccfcd476e2b8e0ab5d9",
        ),
        "setuptools": (
            "80.9.0",
            "062d34222ad13e0cc312a4c02d73f059e86a4acbfbdea8f8f76b28c99f306922",
        ),
        "wheel": (
            "0.45.1",
            "708e7481cc80179af0e556bbf0cc00b8444c7321e2700b8d8580231d13017248",
        ),
    }
    build_lock = PROJECT_ROOT / "requirements-build.lock"

    locked = {}
    for line in build_lock.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        matched = re.fullmatch(
            r"([a-z0-9-]+)==([0-9]+(?:\.[0-9]+)*) "
            r"--hash=sha256:([0-9a-f]{64})",
            stripped,
        )
        assert matched is not None
        name, version, digest = matched.groups()
        assert name not in locked
        locked[name] = (version, digest)

    assert locked == expected


@pytest.mark.parametrize(
    "retired_path",
    [
        "tests/e2e/README.md",
        "tests/e2e/run_live_e2e.sh",
        "tests/e2e/test_verify_live_deliverables.py",
        "tests/e2e/verify_live_deliverables.py",
    ],
)
def test_repository_does_not_advertise_the_retired_delivery_workflow(
    retired_path,
):
    assert not (PROJECT_ROOT / retired_path).exists()


def test_release_metadata_does_not_advertise_retired_business_modes():
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    matched = re.search(
        r'^description\s*=\s*"([^"]+)"$',
        pyproject,
        re.MULTILINE,
    )

    assert matched is not None
    description = matched.group(1).casefold()

    assert "single" not in description
    assert "batch" not in description
