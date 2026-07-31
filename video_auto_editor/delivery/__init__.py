"""标准交付物的构建、验证与发布能力。"""

from ._build import DeliveryBuild
from ._model import DeliveryBuildFailure, DeliveryBuildRequest
from ._publication import Publication, PublicationFailure
from ._verification import (
    DeliveryManifestReader,
    DeliveryManifestReadReason,
    DeliveryManifestReadResult,
    DeliveryManifestReadState,
    DeliveryManifestSummary,
    DeliveryVerification,
    DeliveryVerificationFailure,
)
from .capability import (
    PublishedDelivery,
    UnverifiedDelivery,
    VerifiedDelivery,
)

__all__ = [
    "DeliveryBuild",
    "DeliveryBuildFailure",
    "DeliveryBuildRequest",
    "DeliveryManifestReader",
    "DeliveryManifestReadReason",
    "DeliveryManifestReadResult",
    "DeliveryManifestReadState",
    "DeliveryManifestSummary",
    "DeliveryVerification",
    "DeliveryVerificationFailure",
    "Publication",
    "PublicationFailure",
    "PublishedDelivery",
    "UnverifiedDelivery",
    "VerifiedDelivery",
]
