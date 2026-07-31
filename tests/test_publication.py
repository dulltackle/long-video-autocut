import errno
import hashlib
import multiprocessing
import os
import signal

import pytest

import video_auto_editor.workspace._workspace as workspace_effects
from video_auto_editor.delivery import Publication, PublicationFailure
from video_auto_editor.delivery.capability import (
    UnverifiedDelivery,
    VerifiedDelivery,
)
from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationSource,
)
from video_auto_editor.runtime.errors import ErrorCode
from video_auto_editor.runtime.identity import RunId
from video_auto_editor.workspace import Workspace


def _open_run(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    return workspace.acquire_run(RunId.new())


def _snapshot(contents_by_path):
    digest = hashlib.sha256(b"delivery_snapshot.v1\0")
    for path in sorted(contents_by_path):
        path_bytes = path.encode("utf-8")
        contents = contents_by_path[path]
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(hashlib.sha256(contents).digest())
    return "sha256:" + digest.hexdigest()


def _verified_delivery(run_workspace, contents_by_path):
    staging = run_workspace.delivery_staging
    for path, contents in contents_by_path.items():
        staging.location(path).publish_bytes_atomically(contents)
    unverified = UnverifiedDelivery._from_build(
        run_workspace.run_id,
        staging,
    )
    return VerifiedDelivery._from_verification(
        unverified,
        verification_snapshot=_snapshot(contents_by_path),
        verification_tree=staging.inspect_tree(),
    )


def _commit(delivery, effect):
    del delivery
    effect()


def _crash_after_first_overwrite_exchange(source, workspace):
    run_workspace = Workspace.open(source, workspace).acquire_run(RunId.new())
    verified = _verified_delivery(
        run_workspace,
        {"manifest.json": b"new delivery"},
    )
    original_exchange = workspace_effects._exchange_publication_directories
    exchange_calls = 0

    def exit_after_backup(*args):
        nonlocal exchange_calls
        original_exchange(*args)
        exchange_calls += 1
        if exchange_calls == 1:
            os._exit(91)

    workspace_effects._exchange_publication_directories = exit_after_backup
    Publication.publish(
        verified,
        published_directory=run_workspace.published_delivery,
        previous_directory=run_workspace.previous_delivery,
        overwrite=True,
        cancellation=CancellationSource().token,
        commit=_commit,
    )


def _crash_before_publication_journal_rename(source, workspace):
    run_workspace = Workspace.open(source, workspace).acquire_run(RunId.new())
    verified = _verified_delivery(
        run_workspace,
        {"manifest.json": b"new delivery"},
    )
    original_rename = workspace_effects._rename_no_replace

    def exit_before_journal_publish(*args):
        if args[-1] == workspace_effects._PUBLICATION_JOURNAL_NAME:
            os._exit(92)
        original_rename(*args)

    workspace_effects._rename_no_replace = exit_before_journal_publish
    Publication.publish(
        verified,
        published_directory=run_workspace.published_delivery,
        previous_directory=run_workspace.previous_delivery,
        overwrite=True,
        cancellation=CancellationSource().token,
        commit=_commit,
    )


def _regular_files(directory):
    return {
        entry.relative_path: directory.location(entry.relative_path).read_bytes()
        for entry in directory.inspect_tree()
        if entry.byte_length is not None
    }


def _assert_unchanged(
    run_workspace,
    *,
    current_contents,
    previous_contents,
    staging_contents,
):
    assert (
        _regular_files(run_workspace.published_delivery)
        == current_contents
    )
    assert (
        _regular_files(run_workspace.previous_delivery)
        == previous_contents
    )
    assert (
        _regular_files(run_workspace.delivery_staging)
        == staging_contents
    )


def test_empty_current_delivery_becomes_fully_visible_in_one_publication(
    tmp_path,
):
    with _open_run(tmp_path) as run_workspace:
        contents = {
            "manifest.json": b'{"run_id":"current"}\n',
            "clips/short.mp4": b"complete-media",
        }
        run_workspace.delivery_staging.location("clips").mkdir()
        verified = _verified_delivery(run_workspace, contents)

        published = Publication.publish(
            verified,
            published_directory=run_workspace.published_delivery,
            previous_directory=run_workspace.previous_delivery,
            overwrite=False,
            cancellation=CancellationSource().token,
            commit=_commit,
        )

        assert published.run_id is run_workspace.run_id
        assert published.verification_snapshot == verified.verification_snapshot
        assert (
            published.managed_directory
            is run_workspace.published_delivery
        )
        assert _regular_files(published.managed_directory) == contents
        assert run_workspace.previous_delivery.inspect_tree() == ()


def test_nonempty_current_delivery_is_rejected_without_explicit_overwrite(
    tmp_path,
):
    with _open_run(tmp_path) as run_workspace:
        current_contents = {"manifest.json": b"existing-delivery"}
        run_workspace.published_delivery.location(
            "manifest.json"
        ).publish_bytes_atomically(current_contents["manifest.json"])
        verified = _verified_delivery(
            run_workspace,
            {"manifest.json": b"new-delivery"},
        )

        with pytest.raises(PublicationFailure) as raised:
            Publication.publish(
                verified,
                published_directory=run_workspace.published_delivery,
                previous_directory=run_workspace.previous_delivery,
                overwrite=False,
                cancellation=CancellationSource().token,
                commit=_commit,
            )

        assert raised.value.error_code is ErrorCode.PUBLICATION_COMMIT_FAILED
        assert raised.value.diagnostics == {
            "operation": "publication.verify_binding",
            "reason_code": "publication.destination_not_empty",
        }
        assert (
            _regular_files(run_workspace.published_delivery)
            == current_contents
        )
        assert run_workspace.previous_delivery.inspect_tree() == ()


def test_explicit_overwrite_replaces_current_and_keeps_only_adjacent_version(
    tmp_path,
):
    with _open_run(tmp_path) as run_workspace:
        older_contents = {"manifest.json": b"older-delivery"}
        current_contents = {"manifest.json": b"current-delivery"}
        new_contents = {"manifest.json": b"new-delivery"}
        run_workspace.previous_delivery.location(
            "manifest.json"
        ).publish_bytes_atomically(older_contents["manifest.json"])
        run_workspace.published_delivery.location(
            "manifest.json"
        ).publish_bytes_atomically(current_contents["manifest.json"])
        verified = _verified_delivery(run_workspace, new_contents)

        published = Publication.publish(
            verified,
            published_directory=run_workspace.published_delivery,
            previous_directory=run_workspace.previous_delivery,
            overwrite=True,
            cancellation=CancellationSource().token,
            commit=_commit,
        )

        assert _regular_files(published.managed_directory) == new_contents
        assert (
            _regular_files(run_workspace.previous_delivery)
            == current_contents
        )
        assert set(run_workspace.published_delivery.inspect_tree()) == set(
            published.managed_directory.inspect_tree()
        )
        assert sorted(
            path.name
            for path in (tmp_path / "workspace").iterdir()
            if path.is_dir()
        ) == ["delivery", "delivery.previous", "work"]


def test_publication_rejects_delivery_changed_after_verification(tmp_path):
    with _open_run(tmp_path) as run_workspace:
        verified = _verified_delivery(
            run_workspace,
            {"manifest.json": b"verified-delivery"},
        )
        run_workspace.delivery_staging.location("manifest.json").write_bytes(
            b"changed-after-verification"
        )

        with pytest.raises(PublicationFailure) as raised:
            Publication.publish(
                verified,
                published_directory=run_workspace.published_delivery,
                previous_directory=run_workspace.previous_delivery,
                overwrite=False,
                cancellation=CancellationSource().token,
                commit=_commit,
            )

        assert raised.value.error_code is ErrorCode.PUBLICATION_COMMIT_FAILED
        assert raised.value.diagnostics == {
            "operation": "publication.verify_snapshot",
            "reason_code": "publication.snapshot_changed",
        }
        assert run_workspace.published_delivery.inspect_tree() == ()
        assert run_workspace.previous_delivery.inspect_tree() == ()


def test_commit_callback_cannot_change_verified_tree_before_exchange(
    tmp_path,
):
    with _open_run(tmp_path) as run_workspace:
        verified = _verified_delivery(
            run_workspace,
            {"manifest.json": b"verified-delivery"},
        )

        def change_before_effect(delivery, effect):
            del delivery
            run_workspace.delivery_staging.location(
                "manifest.json"
            ).write_bytes(b"changed-inside-commit")
            effect()

        with pytest.raises(PublicationFailure) as raised:
            Publication.publish(
                verified,
                published_directory=run_workspace.published_delivery,
                previous_directory=run_workspace.previous_delivery,
                overwrite=False,
                cancellation=CancellationSource().token,
                commit=change_before_effect,
            )

        assert raised.value.diagnostics == {
            "operation": "publication.verify_snapshot",
            "reason_code": "publication.snapshot_changed",
        }
        assert run_workspace.published_delivery.inspect_tree() == ()
        assert run_workspace.previous_delivery.inspect_tree() == ()


def test_commit_callback_cannot_replace_workspace_delivery_binding(
    tmp_path,
):
    with _open_run(tmp_path) as run_workspace:
        verified = _verified_delivery(
            run_workspace,
            {"manifest.json": b"verified-delivery"},
        )
        workspace = tmp_path / "workspace"
        published_path = workspace / "delivery"
        displaced_path = workspace / "delivery.displaced"

        def replace_current_before_effect(delivery, effect):
            del delivery
            published_path.rename(displaced_path)
            published_path.mkdir()
            effect()

        try:
            with pytest.raises(PublicationFailure) as raised:
                Publication.publish(
                    verified,
                    published_directory=run_workspace.published_delivery,
                    previous_directory=run_workspace.previous_delivery,
                    overwrite=False,
                    cancellation=CancellationSource().token,
                    commit=replace_current_before_effect,
                )
        finally:
            published_path.rmdir()
            displaced_path.rename(published_path)

        assert raised.value.error_code is ErrorCode.PUBLICATION_COMMIT_FAILED
        assert raised.value.diagnostics == {
            "operation": "publication.verify_binding",
            "reason_code": "publication.binding_changed",
        }
        assert run_workspace.published_delivery.inspect_tree() == ()
        assert run_workspace.previous_delivery.inspect_tree() == ()
        assert _regular_files(run_workspace.delivery_staging) == {
            "manifest.json": b"verified-delivery"
        }


def test_failed_commit_callback_cannot_retain_a_publication_proof(tmp_path):
    with _open_run(tmp_path) as run_workspace:
        verified = _verified_delivery(
            run_workspace,
            {"manifest.json": b"verified-delivery"},
        )
        leaked = []

        def retain_then_fail(delivery, effect):
            del effect
            with pytest.raises(
                TypeError,
                match="PublishedDelivery 只能由 Publication 创建",
            ):
                _ = delivery.managed_directory
            leaked.append(delivery)
            raise RuntimeError("injected callback failure")

        with pytest.raises(RuntimeError, match="callback failure"):
            Publication.publish(
                verified,
                published_directory=run_workspace.published_delivery,
                previous_directory=run_workspace.previous_delivery,
                overwrite=False,
                cancellation=CancellationSource().token,
                commit=retain_then_fail,
            )

        assert len(leaked) == 1
        with pytest.raises(
            TypeError,
            match="PublishedDelivery 只能由 Publication 创建",
        ):
            _ = leaked[0].run_id
        assert run_workspace.published_delivery.inspect_tree() == ()
        assert run_workspace.previous_delivery.inspect_tree() == ()


def test_commit_callback_cannot_invoke_a_retained_effect_after_return(
    tmp_path,
):
    with _open_run(tmp_path) as run_workspace:
        verified = _verified_delivery(
            run_workspace,
            {"manifest.json": b"verified-delivery"},
        )
        retained_effects = []

        def retain_without_commit(delivery, effect):
            del delivery
            retained_effects.append(effect)

        with pytest.raises(
            RuntimeError,
            match="没有执行提交效果",
        ):
            Publication.publish(
                verified,
                published_directory=run_workspace.published_delivery,
                previous_directory=run_workspace.previous_delivery,
                overwrite=False,
                cancellation=CancellationSource().token,
                commit=retain_without_commit,
            )

        assert len(retained_effects) == 1
        with pytest.raises(
            RuntimeError,
            match="只能在提交回调作用域内执行",
        ):
            retained_effects[0]()
        assert run_workspace.published_delivery.inspect_tree() == ()
        assert run_workspace.previous_delivery.inspect_tree() == ()


def test_cancellation_before_commit_rolls_back_and_remains_interrupted(
    tmp_path,
):
    with _open_run(tmp_path) as run_workspace:
        current_contents = {"manifest.json": b"current-delivery"}
        run_workspace.published_delivery.location(
            "manifest.json"
        ).publish_bytes_atomically(current_contents["manifest.json"])
        verified = _verified_delivery(
            run_workspace,
            {"manifest.json": b"new-delivery"},
        )
        cancellation = CancellationSource()

        def interrupt_before_effect(delivery, effect):
            del delivery
            cancellation.request(signal.SIGINT)
            effect()

        with pytest.raises(CancellationRequested) as raised:
            Publication.publish(
                verified,
                published_directory=run_workspace.published_delivery,
                previous_directory=run_workspace.previous_delivery,
                overwrite=True,
                cancellation=cancellation.token,
                commit=interrupt_before_effect,
            )

        assert raised.value.signal_number == signal.SIGINT
        assert (
            _regular_files(run_workspace.published_delivery)
            == current_contents
        )
        assert run_workspace.previous_delivery.inspect_tree() == ()


def test_cancellation_after_commit_keeps_publication_successful(tmp_path):
    with _open_run(tmp_path) as run_workspace:
        new_contents = {"manifest.json": b"new-delivery"}
        verified = _verified_delivery(run_workspace, new_contents)
        cancellation = CancellationSource()

        def interrupt_after_effect(delivery, effect):
            del delivery
            effect()
            cancellation.request(signal.SIGTERM)
            cancellation.token.raise_if_cancelled()

        published = Publication.publish(
            verified,
            published_directory=run_workspace.published_delivery,
            previous_directory=run_workspace.previous_delivery,
            overwrite=False,
            cancellation=cancellation.token,
            commit=interrupt_after_effect,
        )

        assert _regular_files(published.managed_directory) == new_contents
        assert cancellation.token.cancelled


def test_backup_exchange_failure_restores_all_three_directories(
    tmp_path,
    monkeypatch,
):
    with _open_run(tmp_path) as run_workspace:
        current = {"manifest.json": b"current"}
        previous = {"manifest.json": b"previous"}
        staging = {"manifest.json": b"staging"}
        run_workspace.published_delivery.location(
            "manifest.json"
        ).publish_bytes_atomically(current["manifest.json"])
        run_workspace.previous_delivery.location(
            "manifest.json"
        ).publish_bytes_atomically(previous["manifest.json"])
        verified = _verified_delivery(run_workspace, staging)

        def fail_backup(*_args):
            raise OSError("injected backup failure")

        monkeypatch.setattr(
            workspace_effects,
            "_exchange_publication_directories",
            fail_backup,
        )

        with pytest.raises(PublicationFailure) as raised:
            Publication.publish(
                verified,
                published_directory=run_workspace.published_delivery,
                previous_directory=run_workspace.previous_delivery,
                overwrite=True,
                cancellation=CancellationSource().token,
                commit=_commit,
            )

        assert raised.value.error_code is ErrorCode.PUBLICATION_BACKUP_FAILED
        _assert_unchanged(
            run_workspace,
            current_contents=current,
            previous_contents=previous,
            staging_contents=staging,
        )


def test_current_directory_exchange_failure_reverses_prepared_backup(
    tmp_path,
    monkeypatch,
):
    with _open_run(tmp_path) as run_workspace:
        current = {"manifest.json": b"current"}
        previous = {"manifest.json": b"previous"}
        staging = {"manifest.json": b"staging"}
        run_workspace.published_delivery.location(
            "manifest.json"
        ).publish_bytes_atomically(current["manifest.json"])
        run_workspace.previous_delivery.location(
            "manifest.json"
        ).publish_bytes_atomically(previous["manifest.json"])
        verified = _verified_delivery(run_workspace, staging)
        original_exchange = (
            workspace_effects._exchange_publication_directories
        )
        calls = 0

        def fail_current_exchange(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected commit exchange failure")
            original_exchange(*args)

        monkeypatch.setattr(
            workspace_effects,
            "_exchange_publication_directories",
            fail_current_exchange,
        )

        with pytest.raises(PublicationFailure) as raised:
            Publication.publish(
                verified,
                published_directory=run_workspace.published_delivery,
                previous_directory=run_workspace.previous_delivery,
                overwrite=True,
                cancellation=CancellationSource().token,
                commit=_commit,
            )

        assert raised.value.error_code is ErrorCode.PUBLICATION_COMMIT_FAILED
        assert raised.value.diagnostics["reason_code"] == (
            "publication.atomic_replace_failed"
        )
        _assert_unchanged(
            run_workspace,
            current_contents=current,
            previous_contents=previous,
            staging_contents=staging,
        )


def test_unexpected_failure_after_backup_exchange_restores_initial_state(
    tmp_path,
    monkeypatch,
):
    with _open_run(tmp_path) as run_workspace:
        current = {"manifest.json": b"current"}
        previous = {"manifest.json": b"previous"}
        staging = {"manifest.json": b"staging"}
        run_workspace.published_delivery.location(
            "manifest.json"
        ).publish_bytes_atomically(current["manifest.json"])
        run_workspace.previous_delivery.location(
            "manifest.json"
        ).publish_bytes_atomically(previous["manifest.json"])
        verified = _verified_delivery(run_workspace, staging)
        original_exchange = (
            workspace_effects._exchange_publication_directories
        )
        calls = 0

        def fail_after_backup(*args):
            nonlocal calls
            original_exchange(*args)
            calls += 1
            if calls == 1:
                raise RuntimeError("injected unexpected failure")

        monkeypatch.setattr(
            workspace_effects,
            "_exchange_publication_directories",
            fail_after_backup,
        )

        with pytest.raises(PublicationFailure) as raised:
            Publication.publish(
                verified,
                published_directory=run_workspace.published_delivery,
                previous_directory=run_workspace.previous_delivery,
                overwrite=True,
                cancellation=CancellationSource().token,
                commit=_commit,
            )

        assert raised.value.error_code is ErrorCode.PUBLICATION_BACKUP_FAILED
        _assert_unchanged(
            run_workspace,
            current_contents=current,
            previous_contents=previous,
            staging_contents=staging,
        )


def test_parent_directory_sync_failure_rolls_back_committed_exchange(
    tmp_path,
    monkeypatch,
):
    with _open_run(tmp_path) as run_workspace:
        current = {"manifest.json": b"current"}
        previous = {"manifest.json": b"previous"}
        staging = {"manifest.json": b"staging"}
        run_workspace.published_delivery.location(
            "manifest.json"
        ).publish_bytes_atomically(current["manifest.json"])
        run_workspace.previous_delivery.location(
            "manifest.json"
        ).publish_bytes_atomically(previous["manifest.json"])
        verified = _verified_delivery(run_workspace, staging)
        original_sync = workspace_effects._sync_publication_directory
        calls = 0

        def fail_final_sync(descriptor):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("injected parent sync failure")
            original_sync(descriptor)

        monkeypatch.setattr(
            workspace_effects,
            "_sync_publication_directory",
            fail_final_sync,
        )

        with pytest.raises(PublicationFailure) as raised:
            Publication.publish(
                verified,
                published_directory=run_workspace.published_delivery,
                previous_directory=run_workspace.previous_delivery,
                overwrite=True,
                cancellation=CancellationSource().token,
                commit=_commit,
            )

        assert raised.value.error_code is ErrorCode.PUBLICATION_COMMIT_FAILED
        assert raised.value.diagnostics == {
            "operation": "publication.sync",
            "reason_code": "publication.directory_sync_failed",
        }
        _assert_unchanged(
            run_workspace,
            current_contents=current,
            previous_contents=previous,
            staging_contents=staging,
        )


def test_next_run_recovers_a_process_crash_after_backup_exchange(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace_path = tmp_path / "workspace"
    workspace = Workspace.open(source, workspace_path)
    current = workspace_path / "delivery" / "current.txt"
    previous = workspace_path / "delivery.previous" / "previous.txt"
    current.write_bytes(b"current delivery")
    previous.write_bytes(b"previous delivery")
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_after_first_overwrite_exchange,
        args=(source, workspace_path),
    )
    process.start()
    process.join(30)
    try:
        assert process.exitcode == 91

        with workspace.acquire_run(RunId.new()):
            pass

        assert current.read_bytes() == b"current delivery"
        assert previous.read_bytes() == b"previous delivery"
        assert sorted(path.name for path in current.parent.iterdir()) == [
            "current.txt"
        ]
        assert sorted(path.name for path in previous.parent.iterdir()) == [
            "previous.txt"
        ]
    finally:
        if process.is_alive():
            process.terminate()
            process.join(10)


def test_incomplete_journal_write_never_looks_like_a_recovery_record(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace_path = tmp_path / "workspace"
    workspace = Workspace.open(source, workspace_path)
    current = workspace_path / "delivery" / "current.txt"
    previous = workspace_path / "delivery.previous" / "previous.txt"
    current.write_bytes(b"current delivery")
    previous.write_bytes(b"previous delivery")
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_before_publication_journal_rename,
        args=(source, workspace_path),
    )
    process.start()
    process.join(30)
    try:
        assert process.exitcode == 92
        stale_runs = list((workspace_path / "work" / "tmp").iterdir())
        assert len(stale_runs) == 1
        assert not (
            stale_runs[0] / ".publication-transaction.json"
        ).exists()
        assert any(
            path.name.startswith(".publication-journal-create-")
            for path in stale_runs[0].iterdir()
        )

        with workspace.acquire_run(RunId.new()):
            pass

        assert current.read_bytes() == b"current delivery"
        assert previous.read_bytes() == b"previous delivery"
        assert not stale_runs[0].exists()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(10)


def test_exchange_error_after_kernel_change_is_detected_and_rolled_back(
    tmp_path,
    monkeypatch,
):
    with _open_run(tmp_path) as run_workspace:
        staging = {"manifest.json": b"staging"}
        verified = _verified_delivery(run_workspace, staging)
        original_exchange = (
            workspace_effects._exchange_publication_directories
        )
        calls = 0

        def report_error_after_exchange(*args):
            nonlocal calls
            calls += 1
            original_exchange(*args)
            if calls == 1:
                raise OSError("injected post-exchange failure")

        monkeypatch.setattr(
            workspace_effects,
            "_exchange_publication_directories",
            report_error_after_exchange,
        )

        with pytest.raises(PublicationFailure) as raised:
            Publication.publish(
                verified,
                published_directory=run_workspace.published_delivery,
                previous_directory=run_workspace.previous_delivery,
                overwrite=False,
                cancellation=CancellationSource().token,
                commit=_commit,
            )

        assert raised.value.error_code is ErrorCode.PUBLICATION_COMMIT_FAILED
        _assert_unchanged(
            run_workspace,
            current_contents={},
            previous_contents={},
            staging_contents=staging,
        )


def test_commit_state_inspection_failure_rolls_back_without_half_product(
    tmp_path,
    monkeypatch,
):
    with _open_run(tmp_path) as run_workspace:
        current = {"manifest.json": b"current"}
        previous = {"manifest.json": b"previous"}
        staging = {"manifest.json": b"staging"}
        run_workspace.published_delivery.location(
            "manifest.json"
        ).publish_bytes_atomically(current["manifest.json"])
        run_workspace.previous_delivery.location(
            "manifest.json"
        ).publish_bytes_atomically(previous["manifest.json"])
        verified = _verified_delivery(run_workspace, staging)
        original_inspection = workspace_effects._inspect_layout_descriptor

        def fail_layout_inspection(*args, **kwargs):
            if kwargs.get("expected") is None:
                raise OSError("injected layout inspection failure")
            return original_inspection(*args, **kwargs)

        monkeypatch.setattr(
            workspace_effects,
            "_inspect_layout_descriptor",
            fail_layout_inspection,
        )

        with pytest.raises(PublicationFailure) as raised:
            Publication.publish(
                verified,
                published_directory=run_workspace.published_delivery,
                previous_directory=run_workspace.previous_delivery,
                overwrite=True,
                cancellation=CancellationSource().token,
                commit=_commit,
            )

        assert raised.value.error_code is ErrorCode.PUBLICATION_COMMIT_FAILED
        assert raised.value.diagnostics["reason_code"] == (
            "publication.commit_state_uncertain"
        )
        _assert_unchanged(
            run_workspace,
            current_contents=current,
            previous_contents=previous,
            staging_contents=staging,
        )


def test_transaction_journal_cleanup_failure_rolls_back_without_half_product(
    tmp_path,
    monkeypatch,
):
    with _open_run(tmp_path) as run_workspace:
        current = {"manifest.json": b"current"}
        previous = {"manifest.json": b"previous"}
        staging = {"manifest.json": b"staging"}
        run_workspace.published_delivery.location(
            "manifest.json"
        ).publish_bytes_atomically(current["manifest.json"])
        run_workspace.previous_delivery.location(
            "manifest.json"
        ).publish_bytes_atomically(previous["manifest.json"])
        verified = _verified_delivery(run_workspace, staging)
        original_remove = workspace_effects._remove_publication_journal
        remove_calls = 0

        def fail_commit_cleanup(*args, **kwargs):
            nonlocal remove_calls
            remove_calls += 1
            if remove_calls == 1:
                raise OSError("injected publication cleanup failure")
            original_remove(*args, **kwargs)

        monkeypatch.setattr(
            workspace_effects,
            "_remove_publication_journal",
            fail_commit_cleanup,
        )

        with pytest.raises(PublicationFailure) as raised:
            Publication.publish(
                verified,
                published_directory=run_workspace.published_delivery,
                previous_directory=run_workspace.previous_delivery,
                overwrite=True,
                cancellation=CancellationSource().token,
                commit=_commit,
            )

        assert raised.value.error_code is ErrorCode.PUBLICATION_COMMIT_FAILED
        assert raised.value.diagnostics == {
            "operation": "publication.commit",
            "reason_code": "publication.commit_state_uncertain",
        }
        assert remove_calls == 2
        _assert_unchanged(
            run_workspace,
            current_contents=current,
            previous_contents=previous,
            staging_contents=staging,
        )


def test_descriptor_cleanup_error_before_commit_rolls_back_exchange(
    tmp_path,
    monkeypatch,
):
    with _open_run(tmp_path) as run_workspace:
        staging = {"manifest.json": b"staging"}
        verified = _verified_delivery(run_workspace, staging)
        original_exchange = (
            workspace_effects._exchange_publication_directories
        )
        original_close = os.close
        exchanged = False
        close_error_injected = False

        def observe_exchange(*args):
            nonlocal exchanged
            original_exchange(*args)
            exchanged = True

        def fail_first_close_after_exchange(descriptor):
            nonlocal close_error_injected
            if exchanged and not close_error_injected:
                # 提交状态复核中的 mountinfo 描述符仍使用可报告失败的
                # 原始 close；提交后的受管目录 close 才会容忍 Linux
                # 无法判定描述符是否已关闭的错误。
                close_error_injected = True
                original_close(descriptor)
                raise OSError(errno.EIO, "injected close failure")
            original_close(descriptor)

        monkeypatch.setattr(
            workspace_effects,
            "_exchange_publication_directories",
            observe_exchange,
        )
        monkeypatch.setattr(os, "close", fail_first_close_after_exchange)

        with pytest.raises(PublicationFailure) as raised:
            Publication.publish(
                verified,
                published_directory=run_workspace.published_delivery,
                previous_directory=run_workspace.previous_delivery,
                overwrite=False,
                cancellation=CancellationSource().token,
                commit=_commit,
            )

        assert close_error_injected
        assert raised.value.error_code is ErrorCode.PUBLICATION_COMMIT_FAILED
        _assert_unchanged(
            run_workspace,
            current_contents={},
            previous_contents={},
            staging_contents=staging,
        )


def test_rollback_sync_failure_reports_uncertain_durability_after_restoring_names(
    tmp_path,
    monkeypatch,
):
    with _open_run(tmp_path) as run_workspace:
        current = {"manifest.json": b"current"}
        previous = {"manifest.json": b"previous"}
        staging = {"manifest.json": b"staging"}
        run_workspace.published_delivery.location(
            "manifest.json"
        ).publish_bytes_atomically(current["manifest.json"])
        run_workspace.previous_delivery.location(
            "manifest.json"
        ).publish_bytes_atomically(previous["manifest.json"])
        verified = _verified_delivery(run_workspace, staging)
        original_inspection = workspace_effects._inspect_layout_descriptor
        original_sync = workspace_effects._sync_publication_directory
        sync_count = 0

        def fail_commit_inspection(*args, **kwargs):
            if kwargs.get("expected") is None:
                raise OSError("injected commit inspection failure")
            return original_inspection(*args, **kwargs)

        def fail_rollback_sync(descriptor):
            nonlocal sync_count
            sync_count += 1
            if sync_count == 5:
                raise OSError("injected rollback sync failure")
            original_sync(descriptor)

        monkeypatch.setattr(
            workspace_effects,
            "_inspect_layout_descriptor",
            fail_commit_inspection,
        )
        monkeypatch.setattr(
            workspace_effects,
            "_sync_publication_directory",
            fail_rollback_sync,
        )

        with pytest.raises(PublicationFailure) as raised:
            Publication.publish(
                verified,
                published_directory=run_workspace.published_delivery,
                previous_directory=run_workspace.previous_delivery,
                overwrite=True,
                cancellation=CancellationSource().token,
                commit=_commit,
            )

        assert raised.value.error_code is ErrorCode.PUBLICATION_ROLLBACK_FAILED
        assert raised.value.diagnostics == {
            "operation": "publication.sync",
            "reason_code": "publication.directory_sync_failed",
        }
        _assert_unchanged(
            run_workspace,
            current_contents=current,
            previous_contents=previous,
            staging_contents=staging,
        )
