"""只供仓库外已安装 CLI 验收使用的确定性组合根。"""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import time
from importlib import metadata
from pathlib import Path

import video_auto_editor
from video_auto_editor.application import LiveApplication, LiveRunRequest
from video_auto_editor.application.live import _RunAssembly
from video_auto_editor.cache.filesystem import initialize_cache_repository
from video_auto_editor.composition import (
    DisclosureSink,
    _ProductionRunAssembly,
    _ProviderLedger,
    _StepAudioDiagnosticBridge,
    _TextModelDiagnosticBridge,
    _generation_settings,
    _initialize_diagnostics,
    _utc_now,
)
from video_auto_editor.configuration import Configuration, LoadedConfiguration
from video_auto_editor.diagnostics import RunDiagnostics
from video_auto_editor.runtime.cancellation import CancellationSource
from video_auto_editor.runtime.errors import ErrorCode
from video_auto_editor.subtitle_optimization import (
    SubtitleOptimization,
    SubtitleOptimizationSettings,
)
from video_auto_editor.text_model import (
    TextGenerationResponse,
    TextModelExecutionFacts,
)
from video_auto_editor.text_model.deterministic import (
    DeterministicTextModel,
    DeterministicTextModelScript,
)
from video_auto_editor.topic_review import TopicReview, TopicReviewSettings
from video_auto_editor.transcription import (
    CacheUse,
    ExecutionFacts,
    ReadinessReport,
    SpeechPresence,
    TranscriptionChunk,
    TranscriptionFailure,
    TranscriptionResult,
)
from video_auto_editor.transcription.deterministic import (
    DeterministicSpeechRecognition,
    DeterministicTranscriptionScript,
)
from video_auto_editor.workspace import (
    DiagnosticRunWorkspace,
    RunWorkspace,
    Workspace,
)


_APPLICATION_VERSION = metadata.version("video-auto-editor")
_TRANSCRIPT_TEXT = "嗯，忠实原文必须保留语气词。"
_PRODUCTION_CREDENTIAL_VARIABLES = ("STEPFUN_API_KEY",)


def _topic_response(*, publish: bool) -> TextGenerationResponse:
    review = (
        {
            "candidate_key": "candidate_1",
            "topic_name": "独立验收",
            "topic_complete": True,
            "learning_value": 9,
            "share_value": 8,
            "publish_ready_score": 95,
            "export_decision": "publish_ready",
            "title": "验证已安装控制台命令",
            "summary": "从仓库外执行真实媒体与标准交付流程。",
            "keywords": ["验收", "标准交付"],
            "needs_human_review": False,
            "reject_reason": "",
            "boundary_fix_suggestion": "",
            "boundary_fix_start_ms": None,
            "boundary_fix_end_ms": None,
        }
        if publish
        else {
            "candidate_key": "candidate_1",
            "topic_name": "上下文不足",
            "topic_complete": False,
            "learning_value": 5,
            "share_value": 4,
            "publish_ready_score": 60,
            "export_decision": "reject",
            "title": "不发布的候选",
            "summary": "完整评审后确认没有独立结论。",
            "keywords": ["评审"],
            "needs_human_review": False,
            "reject_reason": "缺少独立结论",
            "boundary_fix_suggestion": "",
            "boundary_fix_start_ms": None,
            "boundary_fix_end_ms": None,
        }
    )
    return TextGenerationResponse(
        text=json.dumps(
            {"reviews": [review]},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        execution_facts=TextModelExecutionFacts(
            transport_attempt_count=0,
            elapsed_ms=0,
        ),
    )


def _subtitle_response() -> TextGenerationResponse:
    return TextGenerationResponse(
        text="忠实原文必须保留语气词",
        execution_facts=TextModelExecutionFacts(
            transport_attempt_count=0,
            elapsed_ms=0,
        ),
    )


def _speech_recognition(
    scenario: str,
) -> DeterministicSpeechRecognition | _WaitingSpeechRecognition:
    if scenario == "typed_failure":
        return DeterministicSpeechRecognition(
            DeterministicTranscriptionScript.fail(
                TranscriptionFailure(
                    ErrorCode.TRANSCRIPTION_SERVICE_UNAVAILABLE,
                    execution_facts=ExecutionFacts(
                        cache_use=CacheUse.MISS,
                    ),
                    diagnostics={"attempt": 1, "http_status": 503},
                )
            )
        )
    if scenario in {"sigint", "sigterm"}:
        return _WaitingSpeechRecognition()
    return DeterministicSpeechRecognition(
        DeterministicTranscriptionScript.succeed(
            TranscriptionResult(
                chunks=(
                    TranscriptionChunk(
                        start_ms=200,
                        end_ms=4_800,
                        text=_TRANSCRIPT_TEXT,
                    ),
                ),
                speech_presence=SpeechPresence.PRESENT,
                execution_facts=ExecutionFacts(cache_use=CacheUse.MISS),
            )
        )
    )


def _write_rendezvous(variable: str) -> None:
    destination_name = os.environ.get(variable)
    if not destination_name:
        raise RuntimeError("已安装 CLI 验收缺少信号 rendezvous 路径")
    _write_atomic(Path(destination_name), b"ready\n")


def _write_atomic(destination: Path, contents: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    parent = destination.parent.resolve(strict=True)
    target = parent / destination.name
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class _WaitingSpeechRecognition:
    __slots__ = ()

    def check_readiness(self) -> ReadinessReport:
        return ReadinessReport(ready=True)

    def transcribe(self, request):
        _write_rendezvous("INSTALLED_ACCEPTANCE_RENDEZVOUS")
        request.cancellation.wait()
        request.cancellation.raise_if_cancelled()
        raise RuntimeError("信号验收未观察到取消")


def _install_publication_signal_rendezvous(*, after_exchange: int) -> None:
    import video_auto_editor.workspace._workspace as workspace_effects

    original = workspace_effects._exchange_publication_directories
    exchange_count = 0

    def exchange(*args):
        nonlocal exchange_count
        original(*args)
        exchange_count += 1
        if exchange_count != after_exchange:
            return
        _write_rendezvous("INSTALLED_ACCEPTANCE_RENDEZVOUS")
        expected = int(os.environ["INSTALLED_ACCEPTANCE_SIGNAL"])
        for _ in range(1_000):
            if expected in signal.sigpending():
                return
            time.sleep(0.01)
        raise RuntimeError("发布信号验收未收到预期信号")

    workspace_effects._exchange_publication_directories = exchange


def _install_repeated_signal_rendezvous():
    original_request = CancellationSource._request_from_signal

    def request(source, signal_number):
        accepted = original_request(source, signal_number)
        if accepted:
            _write_rendezvous("INSTALLED_ACCEPTANCE_FIRST_SIGNAL")
        return accepted

    CancellationSource._request_from_signal = request

    def wait_before_workspace(_source, _workspace_dir):
        _write_rendezvous("INSTALLED_ACCEPTANCE_RENDEZVOUS")
        while True:
            time.sleep(1)

    return wait_before_workspace


class _InstalledAcceptanceAssemblyFactory:
    __slots__ = ("_disclosure_sink", "_scenario")

    def __init__(
        self,
        scenario: str,
        disclosure_sink: DisclosureSink | None,
    ) -> None:
        if scenario not in {
            "clips",
            "empty",
            "postcommit_signal",
            "repeated_signal",
            "rollback",
            "sigint",
            "sigterm",
            "typed_failure",
        }:
            raise ValueError("已安装 CLI 验收场景不受支持")
        self._scenario = scenario
        self._disclosure_sink = disclosure_sink

    def create(
        self,
        *,
        request: LiveRunRequest,
        configuration: LoadedConfiguration,
        run_workspace: RunWorkspace,
    ) -> _RunAssembly:
        effective = configuration.effective
        cache = initialize_cache_repository(
            run_workspace.cache,
            application_version=_APPLICATION_VERSION,
        )
        ledger = _ProviderLedger()
        stepaudio_bridge = _StepAudioDiagnosticBridge(ledger)
        text_bridge = _TextModelDiagnosticBridge(ledger)
        topic_model = DeterministicTextModel(
            DeterministicTextModelScript.succeed(
                _topic_response(publish=self._scenario != "empty")
            )
        )
        subtitle_model = DeterministicTextModel(
            DeterministicTextModelScript.succeed(_subtitle_response())
        )
        topic_review = TopicReview(
            topic_model,
            cache,
            TopicReviewSettings(
                adapter_id=(
                    f"installed-acceptance-deterministic-{self._scenario}"
                ),
                generation=_generation_settings(effective.topic_review),
            ),
        )
        subtitle_optimization = SubtitleOptimization(
            subtitle_model,
            cache,
            SubtitleOptimizationSettings(
                adapter_id=(
                    f"installed-acceptance-deterministic-{self._scenario}"
                ),
                generation=_generation_settings(effective.subtitle_optimization),
                window_max_chars=100,
                max_chars_per_line=effective.subtitle_style.max_chars_per_line,
                max_lines=effective.subtitle_style.max_lines,
            ),
        )
        return _ProductionRunAssembly(
            configuration=configuration,
            run_workspace=run_workspace,
            speech_recognition=_speech_recognition(self._scenario),
            topic_review=topic_review,
            subtitle_optimization=subtitle_optimization,
            stepaudio_bridge=stepaudio_bridge,
            text_bridge=text_bridge,
            ledger=ledger,
            disclosure_sink=self._disclosure_sink,
            overwrite=request.overwrite,
            wall_clock=_utc_now,
        )


def _write_process_audit() -> None:
    destination_name = os.environ.get("INSTALLED_ACCEPTANCE_PROCESS_AUDIT")
    if not destination_name:
        raise RuntimeError("已安装 CLI 验收缺少进程审计路径")
    value = {
        "candidate_package_file": str(
            Path(video_auto_editor.__file__).resolve(strict=True)
        ),
        "console": str(Path(sys.argv[0]).resolve(strict=True)),
        "cwd": str(Path.cwd().resolve(strict=True)),
        "production_credentials_present": [
            name
            for name in _PRODUCTION_CREDENTIAL_VARIABLES
            if os.environ.get(name)
        ],
    }
    _write_atomic(
        Path(destination_name),
        (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
    )


def compose_installed_acceptance_application(
    *,
    scenario: str,
    disclosure_sink: DisclosureSink | None = None,
) -> LiveApplication:
    """装配真实本地能力与确定性供应商端口。"""
    if disclosure_sink is not None and not callable(disclosure_sink):
        raise TypeError("供应商披露接收端必须可调用")
    if any(os.environ.get(name) for name in _PRODUCTION_CREDENTIAL_VARIABLES):
        raise RuntimeError("已安装 CLI 验收不得持有生产凭据")
    _write_process_audit()
    open_workspace = Workspace.open
    if scenario == "rollback":
        _install_publication_signal_rendezvous(after_exchange=1)
    elif scenario == "postcommit_signal":
        _install_publication_signal_rendezvous(after_exchange=2)
    elif scenario == "repeated_signal":
        open_workspace = _install_repeated_signal_rendezvous()
    return LiveApplication._compose(
        assembly_factory=_InstalledAcceptanceAssemblyFactory(
            scenario,
            disclosure_sink,
        ),
        open_workspace=open_workspace,
        open_diagnostic_workspace=Workspace.open_diagnostics,
        load_configuration=Configuration.load,
        initialize_diagnostics=_initialize_diagnostics,
        application_version=_APPLICATION_VERSION,
        wall_clock=_utc_now,
    )


__all__ = ["compose_installed_acceptance_application"]
