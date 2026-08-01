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
