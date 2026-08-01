import importlib.util
import re
from pathlib import Path

import pytest

from video_auto_editor import cli


PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
