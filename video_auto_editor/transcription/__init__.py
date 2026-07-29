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
    ReadinessIssue,
    ReadinessReport,
    SpeechPresence,
    SpeechRecognition,
    TranscriptChunk,
    TranscriptionChunk,
    TranscriptionFailure,
    TranscriptionRequest,
    TranscriptionResult,
)
from .stepaudio import (
    StepAudioSettings,
    StepAudioSpeechRecognition,
)

__all__ = [
    "CacheUse",
    "CharacterSpan",
    "CompleteTranscript",
    "DeterministicSpeechRecognition",
    "DeterministicTranscriptionScript",
    "ExecutionFacts",
    "ReadinessIssue",
    "ReadinessReport",
    "SpeechPresence",
    "SpeechRecognition",
    "StepAudioSettings",
    "StepAudioSpeechRecognition",
    "TranscriptChunk",
    "TranscriptionChunk",
    "TranscriptionFailure",
    "TranscriptionRequest",
    "TranscriptionResult",
]
