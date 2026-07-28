"""供应商无感知的语音识别深模块。"""

from .deterministic import (
    DeterministicSpeechRecognition,
    DeterministicTranscriptionScript,
)
from .interface import (
    CacheUse,
    CharacterSpan,
    CompleteTranscript,
    ExecutionFacts,
    ReadinessReport,
    SpeechPresence,
    SpeechRecognition,
    TranscriptChunk,
    TranscriptionChunk,
    TranscriptionFailure,
    TranscriptionRequest,
    TranscriptionResult,
)

__all__ = [
    "CacheUse",
    "CharacterSpan",
    "CompleteTranscript",
    "DeterministicSpeechRecognition",
    "DeterministicTranscriptionScript",
    "ExecutionFacts",
    "ReadinessReport",
    "SpeechPresence",
    "SpeechRecognition",
    "TranscriptChunk",
    "TranscriptionChunk",
    "TranscriptionFailure",
    "TranscriptionRequest",
    "TranscriptionResult",
]
