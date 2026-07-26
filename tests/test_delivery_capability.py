from dataclasses import FrozenInstanceError

import pytest

from video_auto_editor.delivery.capability import (
    ManagedDirectoryCapability,
    ManagedDirectoryRole,
    PublishedDelivery,
    UnverifiedDelivery,
    VerifiedDelivery,
)
from video_auto_editor.runtime.identity import RunId
from video_auto_editor.workspace import Workspace
from video_auto_editor.workspace import _capability as capability_module


def open_workspace(tmp_path, name="workspace"):
    source = tmp_path / f"{name}.mp4"
    source.write_bytes(b"source")
    return Workspace.open(source, tmp_path / name)


def test_delivery_capability_advances_in_order_and_preserves_its_binding(tmp_path):
    run_id = RunId.new()
    with open_workspace(tmp_path).acquire_run(run_id) as run_workspace:
        staging_directory = run_workspace.delivery_staging
        published_directory = run_workspace.published_delivery

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
    run_id = RunId.new()
    with open_workspace(tmp_path).acquire_run(run_id) as run_workspace:
        unverified = UnverifiedDelivery._from_build(
            run_id,
            run_workspace.delivery_staging,
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
    with open_workspace(tmp_path).acquire_run(RunId.new()) as run_workspace:
        capability = run_workspace.delivery_staging

        with pytest.raises(FrozenInstanceError):
            capability.role = ManagedDirectoryRole.PUBLISHED_DELIVERY

        with pytest.raises(TypeError, match="不能由 Workspace 之外的模块实现"):

            class ForgedManagedDirectory(ManagedDirectoryCapability):
                pass


def test_delivery_rejects_an_object_new_forged_directory_capability():
    forged = object.__new__(ManagedDirectoryCapability)
    object.__setattr__(forged, "role", ManagedDirectoryRole.DELIVERY_STAGING)

    with pytest.raises(TypeError, match="Workspace 签发"):
        UnverifiedDelivery._from_build(RunId.new(), forged)


def test_delivery_rejects_a_role_spoofed_clone_of_a_real_capability(tmp_path):
    run_id = RunId.new()
    with open_workspace(tmp_path).acquire_run(run_id) as run_workspace:
        verified = VerifiedDelivery._from_verification(
            UnverifiedDelivery._from_build(
                run_id,
                run_workspace.delivery_staging,
            ),
            verification_snapshot="snapshot-001",
        )
        source = run_workspace.cache
        forged = object.__new__(ManagedDirectoryCapability)
        object.__setattr__(forged, "_operations", source._operations)
        object.__setattr__(
            forged,
            "_workspace_identity",
            source._workspace_identity,
        )
        object.__setattr__(forged, "_run_id", source._run_id)
        object.__setattr__(forged, "_seal", source._seal)
        object.__setattr__(
            forged,
            "role",
            ManagedDirectoryRole.PUBLISHED_DELIVERY,
        )

        with pytest.raises(TypeError, match="Workspace 签发"):
            PublishedDelivery._from_publication(
                verified,
                published_directory=forged,
            )


def test_private_seal_cannot_register_forged_managed_operations():
    operations = object.__new__(capability_module._ManagedOperations)
    object.__setattr__(
        operations,
        "_use_file",
        lambda _parts, _mode, _effect: None,
    )
    object.__setattr__(
        operations,
        "_validate_directory",
        lambda _parts: None,
    )
    object.__setattr__(
        operations,
        "_make_directory",
        lambda _parts: None,
    )
    object.__setattr__(
        operations,
        "_seal",
        capability_module._CAPABILITY_SEAL,
    )
    forged = object.__new__(ManagedDirectoryCapability)
    object.__setattr__(forged, "_operations", operations)
    object.__setattr__(forged, "_workspace_identity", object())
    object.__setattr__(forged, "_run_id", RunId.new())
    object.__setattr__(
        forged,
        "_seal",
        capability_module._CAPABILITY_SEAL,
    )
    object.__setattr__(
        forged,
        "role",
        ManagedDirectoryRole.DELIVERY_STAGING,
    )

    with pytest.raises(TypeError, match="Workspace 签发"):
        UnverifiedDelivery._from_build(forged._run_id, forged)


def test_delivery_rejects_mutated_operations_on_a_real_capability(tmp_path):
    run_id = RunId.new()
    with open_workspace(tmp_path).acquire_run(run_id) as run_workspace:
        staging = run_workspace.delivery_staging
        object.__setattr__(
            staging._operations,
            "_validate_directory",
            lambda _parts: None,
        )

        with pytest.raises(TypeError, match="Workspace 签发"):
            UnverifiedDelivery._from_build(run_id, staging)


def test_publication_rejects_an_object_new_forged_verified_delivery(tmp_path):
    run_id = RunId.new()
    with open_workspace(tmp_path).acquire_run(run_id) as run_workspace:
        unverified = UnverifiedDelivery._from_build(
            run_id,
            run_workspace.delivery_staging,
        )
        forged = object.__new__(VerifiedDelivery)
        object.__setattr__(forged, "_binding", unverified._binding)
        object.__setattr__(
            forged,
            "verification_snapshot",
            "forged-snapshot",
        )

        with pytest.raises(TypeError, match="DeliveryVerification"):
            PublishedDelivery._from_publication(
                forged,
                published_directory=run_workspace.published_delivery,
            )


def test_publication_rejects_mutated_verified_delivery_binding(tmp_path):
    run_id = RunId.new()
    with open_workspace(tmp_path).acquire_run(run_id) as run_workspace:
        verified = VerifiedDelivery._from_verification(
            UnverifiedDelivery._from_build(
                run_id,
                run_workspace.delivery_staging,
            ),
            verification_snapshot="snapshot-001",
        )
        object.__setattr__(
            verified._binding,
            "managed_directory",
            run_workspace.cache,
        )

        with pytest.raises(TypeError, match="DeliveryVerification"):
            PublishedDelivery._from_publication(
                verified,
                published_directory=run_workspace.published_delivery,
            )


def test_public_delivery_types_do_not_offer_self_asserted_state_transitions():
    assert not hasattr(UnverifiedDelivery, "from_build")
    assert not hasattr(VerifiedDelivery, "from_verification")
    assert not hasattr(PublishedDelivery, "from_publication")


def test_publication_rejects_a_directory_from_another_workspace(tmp_path):
    run_id = RunId.new()
    first_workspace = open_workspace(tmp_path, "workspace-a")
    second_workspace = open_workspace(tmp_path, "workspace-b")
    with (
        first_workspace.acquire_run(run_id) as first_run,
        second_workspace.acquire_run(RunId.new()) as second_run,
    ):
        verified = VerifiedDelivery._from_verification(
            UnverifiedDelivery._from_build(
                run_id,
                first_run.delivery_staging,
            ),
            verification_snapshot="snapshot-001",
        )

        with pytest.raises(ValueError, match="同一个 Workspace"):
            PublishedDelivery._from_publication(
                verified,
                published_directory=second_run.published_delivery,
            )


def test_delivery_states_reject_the_wrong_managed_directory_role(tmp_path):
    run_id = RunId.new()
    with (
        open_workspace(tmp_path).acquire_run(run_id) as run_workspace,
        pytest.raises(ValueError, match="未发布交付暂存目录"),
    ):
        UnverifiedDelivery._from_build(
            run_id,
            run_workspace.published_delivery,
        )


def test_delivery_staging_rejects_a_different_run_binding(tmp_path):
    run_id = RunId.new()
    other_run_id = RunId.new()
    with (
        open_workspace(tmp_path).acquire_run(run_id) as run_workspace,
        pytest.raises(ValueError, match="同一次运行"),
    ):
        UnverifiedDelivery._from_build(
            other_run_id,
            run_workspace.delivery_staging,
        )


def test_delivery_transitions_reject_the_wrong_predecessor_type(tmp_path):
    run_id = RunId.new()
    with open_workspace(tmp_path).acquire_run(run_id) as run_workspace:
        unverified = UnverifiedDelivery._from_build(
            run_id,
            run_workspace.delivery_staging,
        )
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
                published_directory=run_workspace.published_delivery,
            )
