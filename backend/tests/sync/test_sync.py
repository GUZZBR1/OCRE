import pytest

from ocre_core.sync import (
    BatchReceipt,
    SyncBatch,
    SyncBatchIdentityConflict,
    SyncConflict,
    SyncOperation,
    SyncReady,
    SyncReplay,
    check_sync_base,
    evaluate_sync_batch,
)


def batch(**changes):
    values = {
        "project_id": "project-1",
        "batch_id": "batch-1",
        "actor_id": "actor-1",
        "base_revision_id": "rev-1",
        "operations": (SyncOperation("op-1", b"opaque CAD bytes"),),
    }
    values.update(changes)
    return SyncBatch(**values)


def test_matching_base_is_only_ready_for_atomic_append():
    request = batch()
    decision = evaluate_sync_batch(request, current_head_revision_id="rev-1")

    assert decision == SyncReady(request, expected_head_revision_id="rev-1")


def test_stale_base_is_explicit_conflict_and_retains_original_operations():
    request = batch()
    decision = evaluate_sync_batch(request, current_head_revision_id="rev-2")

    assert decision == SyncConflict("project-1", "batch-1", "rev-1", "rev-2")
    assert request.operations == (SyncOperation("op-1", b"opaque CAD bytes"),)


def test_initial_sync_accepts_matching_absent_base_and_head():
    assert isinstance(
        evaluate_sync_batch(batch(base_revision_id=None), current_head_revision_id=None),
        SyncReady,
    )


def test_exact_replay_returns_original_receipt_even_after_head_advances():
    request = batch()
    receipt = BatchReceipt(request, "rev-2")

    decision = evaluate_sync_batch(
        request, current_head_revision_id="rev-9", prior_receipt=receipt
    )

    assert decision == SyncReplay(receipt)


@pytest.mark.parametrize(
    "changes",
    [
        {"actor_id": "actor-2"},
        {"base_revision_id": "rev-0"},
        {"operations": (SyncOperation("op-1", b"changed"),)},
        {"operations": (SyncOperation("op-2", b"opaque CAD bytes"),)},
    ],
)
def test_reusing_batch_identity_with_different_envelope_is_rejected(changes):
    original = batch()
    changed = batch(**changes)
    receipt = BatchReceipt(original, "rev-2")

    assert evaluate_sync_batch(
        changed, current_head_revision_id="rev-1", prior_receipt=receipt
    ) == SyncBatchIdentityConflict("project-1", "batch-1")


@pytest.mark.parametrize(
    "changes",
    [
        {"project_id": ""},
        {"batch_id": ""},
        {"actor_id": ""},
    ],
)
def test_required_batch_identity_fields_must_be_nonempty(changes):
    with pytest.raises(ValueError):
        batch(**changes)


def test_operation_payload_must_remain_opaque_bytes():
    with pytest.raises(TypeError):
        SyncOperation("op-1", {"cad": "not interpreted"})


def test_operation_sequence_must_be_an_immutable_tuple():
    with pytest.raises(TypeError):
        batch(operations=[SyncOperation("op-1", b"x")])


def test_prior_receipt_must_be_scoped_to_requested_batch():
    receipt = BatchReceipt(batch(project_id="another-project"), "rev-2")

    with pytest.raises(ValueError):
        evaluate_sync_batch(batch(), current_head_revision_id="rev-1", prior_receipt=receipt)


def test_legacy_base_check_still_reports_conflict():
    assert check_sync_base(
        project_id="project-1",
        batch_id="batch-1",
        base_revision_id="rev-1",
        current_head_revision_id="rev-2",
    ) == SyncConflict("project-1", "batch-1", "rev-1", "rev-2")
