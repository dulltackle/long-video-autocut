from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from video_auto_editor.delivery.capability import (
    ManagedDirectoryCapability,
    ManagedDirectoryRole,
    PublishedDelivery,
    UnverifiedDelivery,
    VerifiedDelivery,
)
from video_auto_editor.runtime.identity import RunId


def managed_directory(path, workspace_identity, role):
    return ManagedDirectoryCapability._from_workspace(
        path,
        workspace_identity=workspace_identity,
        role=role,
    )


def test_delivery_capability_advances_in_order_and_preserves_its_binding(tmp_path):
    run_id = RunId.new()
    workspace_identity = object()
    staging_directory = managed_directory(
        tmp_path / "delivery-staging",
        workspace_identity,
        ManagedDirectoryRole.DELIVERY_STAGING,
    )
    published_directory = managed_directory(
        tmp_path / "delivery",
        workspace_identity,
        ManagedDirectoryRole.PUBLISHED_DELIVERY,
    )

    unverified = UnverifiedDelivery._from_build(run_id, staging_directory)
    verified = VerifiedDelivery._from_verification(
        unverified,
        verification_snapshot="snapshot-001",
    )
    published = PublishedDelivery._from_publication(
        verified,
        published_directory=published_directory,
    )

    assert unverified.run_id is run_id
    assert verified.run_id is run_id
    assert published.run_id is run_id
    assert unverified.managed_directory is staging_directory
    assert verified.managed_directory is staging_directory
    assert published.managed_directory is published_directory
    assert verified.verification_snapshot == "snapshot-001"
    assert published.verification_snapshot == "snapshot-001"

    with pytest.raises(FrozenInstanceError):
        verified.verification_snapshot = "changed"


def test_verification_snapshot_rejects_non_string_values_clearly(tmp_path):
    unverified = UnverifiedDelivery._from_build(
        RunId.new(),
        managed_directory(
            tmp_path / "delivery-staging",
            object(),
            ManagedDirectoryRole.DELIVERY_STAGING,
        ),
    )

    with pytest.raises(TypeError, match="验证快照标识必须是字符串"):
        VerifiedDelivery._from_verification(
            unverified,
            verification_snapshot=123,
        )


@pytest.mark.parametrize(
    ("delivery_type", "message"),
    [
        (UnverifiedDelivery, "UnverifiedDelivery 只能由 DeliveryBuild"),
        (VerifiedDelivery, "VerifiedDelivery 只能由 DeliveryVerification"),
        (PublishedDelivery, "PublishedDelivery 只能由 Publication"),
    ],
)
def test_delivery_capability_types_cannot_be_constructed_out_of_order(
    delivery_type,
    message,
):
    with pytest.raises(TypeError, match=message):
        delivery_type()


def test_raw_absolute_path_cannot_claim_to_be_a_managed_directory(tmp_path):
    with pytest.raises(TypeError, match="Workspace 签发的受管目录 capability"):
        UnverifiedDelivery._from_build(
            RunId.new(),
            tmp_path / "not-a-capability",
        )


def test_managed_directory_capability_is_sealed_and_deeply_immutable(tmp_path):
    capability = managed_directory(
        tmp_path / "delivery-staging",
        object(),
        ManagedDirectoryRole.DELIVERY_STAGING,
    )

    with pytest.raises(FrozenInstanceError):
        capability._path = Path("/etc")

    with pytest.raises(TypeError, match="不能由 Workspace 之外的模块实现"):

        class ForgedManagedDirectory(ManagedDirectoryCapability):
            pass


def test_public_delivery_types_do_not_offer_self_asserted_state_transitions():
    assert not hasattr(UnverifiedDelivery, "from_build")
    assert not hasattr(VerifiedDelivery, "from_verification")
    assert not hasattr(PublishedDelivery, "from_publication")


def test_publication_rejects_a_directory_from_another_workspace(tmp_path):
    staging = managed_directory(
        tmp_path / "workspace-a" / "delivery-staging",
        object(),
        ManagedDirectoryRole.DELIVERY_STAGING,
    )
    verified = VerifiedDelivery._from_verification(
        UnverifiedDelivery._from_build(RunId.new(), staging),
        verification_snapshot="snapshot-001",
    )
    other_workspace_delivery = managed_directory(
        tmp_path / "workspace-b" / "delivery",
        object(),
        ManagedDirectoryRole.PUBLISHED_DELIVERY,
    )

    with pytest.raises(ValueError, match="同一个 Workspace"):
        PublishedDelivery._from_publication(
            verified,
            published_directory=other_workspace_delivery,
        )


def test_delivery_states_reject_the_wrong_managed_directory_role(tmp_path):
    workspace_identity = object()
    published_directory = managed_directory(
        tmp_path / "delivery",
        workspace_identity,
        ManagedDirectoryRole.PUBLISHED_DELIVERY,
    )

    with pytest.raises(ValueError, match="未发布交付暂存目录"):
        UnverifiedDelivery._from_build(RunId.new(), published_directory)


def test_delivery_transitions_reject_the_wrong_predecessor_type(tmp_path):
    workspace_identity = object()
    staging = managed_directory(
        tmp_path / "delivery-staging",
        workspace_identity,
        ManagedDirectoryRole.DELIVERY_STAGING,
    )
    published_directory = managed_directory(
        tmp_path / "delivery",
        workspace_identity,
        ManagedDirectoryRole.PUBLISHED_DELIVERY,
    )
    unverified = UnverifiedDelivery._from_build(RunId.new(), staging)
    verified = VerifiedDelivery._from_verification(
        unverified,
        verification_snapshot="snapshot-001",
    )

    with pytest.raises(TypeError, match="只能推进 UnverifiedDelivery"):
        VerifiedDelivery._from_verification(
            verified,
            verification_snapshot="snapshot-002",
        )

    with pytest.raises(TypeError, match="只能推进 VerifiedDelivery"):
        PublishedDelivery._from_publication(
            unverified,
            published_directory=published_directory,
        )
