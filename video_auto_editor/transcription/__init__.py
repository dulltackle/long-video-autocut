"""供应商无感知的语音识别深模块。"""

from .interface import (
    CacheUse,
    CharacterSpan,
    CompleteTranscript,
    ExecutionFacts,
    ReadinessIssue,
    ReadinessReport,
    SpeechPresence,
    SpeechRecognition,
    TranscriptChunk,
    TranscriptionChunk,
    TranscriptionFailure,
    TranscriptionRemoteRequestEvent,
    TranscriptionRemoteRequestEventKind,
    TranscriptionRemoteRequestEventSink,
    TranscriptionRequest,
    TranscriptionResult,
)

__all__ = [
    "CacheUse",
    "CharacterSpan",
    "CompleteTranscript",
    "ExecutionFacts",
    "ReadinessIssue",
    "ReadinessReport",
    "SpeechPresence",
    "SpeechRecognition",
    "TranscriptChunk",
    "TranscriptionChunk",
    "TranscriptionFailure",
    "TranscriptionRemoteRequestEvent",
    "TranscriptionRemoteRequestEventKind",
    "TranscriptionRemoteRequestEventSink",
    "TranscriptionRequest",
    "TranscriptionResult",
]
