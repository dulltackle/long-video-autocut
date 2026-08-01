import importlib
import re
from pathlib import Path

import pytest

from video_auto_editor.config import CONFIG
from video_auto_editor.transcript import WhisperConfig, WhisperTranscriber


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("module_name", "retired_names"),
    [
        (
            "video_auto_editor.cli",
            (
                "process_single_video",
                "process_batch",
                "_find_video_files",
                "_can_remove_work_dir",
            ),
        ),
        (
            "video_auto_editor.dedup",
            ("check_duplicate_content", "cross_video_dedup"),
        ),
        ("video_auto_editor.media", ("concat_videos",)),
        ("video_auto_editor.models", ("Segment", "ClipInfo")),
        (
            "video_auto_editor.report",
            ("generate_single_report", "generate_batch_report"),
        ),
        (
            "video_auto_editor.scoring",
            ("_score_boundary", "score_segment", "calculate_adjusted_score"),
        ),
        ("video_auto_editor.selection", ("_fluency_rate", "select_best_segment")),
        ("video_auto_editor.silence", ("identify_segments",)),
        (
            "video_auto_editor.transcript",
            ("TranscriptionResult", "transcribe_candidates"),
        ),
    ],
)
def test_release_modules_do_not_expose_retired_single_or_batch_capabilities(
    module_name,
    retired_names,
):
    module = importlib.import_module(module_name)

    assert [name for name in retired_names if hasattr(module, name)] == []


def test_release_configuration_does_not_accept_retired_single_fields():
    retired_fields = {
        "bonus_completeness_max",
        "min_duration",
        "min_score",
        "whisper_channels",
        "whisper_output_format",
        "whisper_sample_rate",
    }

    assert retired_fields.isdisjoint(CONFIG)
    assert not hasattr(WhisperTranscriber, "transcribe_segment")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("channels", 1),
        ("output_format", "txt"),
        ("sample_rate", 16_000),
    ],
)
def test_whisper_configuration_rejects_retired_segment_fields(field, value):
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        WhisperConfig(**{field: value})


def test_release_metadata_does_not_advertise_retired_business_modes():
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    matched = re.search(r'^description\s*=\s*"([^"]+)"$', pyproject, re.MULTILINE)

    assert matched is not None
    description = matched.group(1).casefold()

    assert "single" not in description
    assert "batch" not in description
