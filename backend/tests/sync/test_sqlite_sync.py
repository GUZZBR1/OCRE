from ocre_core.sync import BatchReceipt, SyncBatch, SyncBatchIdentityConflict, SyncConflict, SyncOperation, SyncReplay
from ocre_core.persistence.sync import SQLiteSyncStore

def batch(**kw):
    values=dict(project_id="p",batch_id="b",actor_id="a",base_revision_id=None,operations=(SyncOperation("o",b"opaque"),))
    values.update(kw); return SyncBatch(**values)

def test_atomic_sync_replay_conflict_identity_and_tenant_scope(tmp_path):
    path=tmp_path/"sync.db"; store=SQLiteSyncStore(path); original=batch()
    receipt=store.commit(original,tenant_id="t1",committed_revision_id="r1",revision_snapshot=b"snapshot")
    assert receipt == BatchReceipt(original,"r1")
    assert store.current_head(tenant_id="t1",project_id="p")=="r1"
    assert store.evaluate(original,tenant_id="t1")==SyncReplay(receipt)
    changed=batch(operations=(SyncOperation("o",b"different"),))
    assert store.commit(changed,tenant_id="t1",committed_revision_id="r2")==SyncBatchIdentityConflict("p","b")
    assert isinstance(store.commit(batch(batch_id="other",base_revision_id="r1"),tenant_id="t1",committed_revision_id="r2"),BatchReceipt)
    assert store.current_head(tenant_id="t1",project_id="p")=="r2"
    assert store.current_head(tenant_id="t2",project_id="p") is None
    assert SQLiteSyncStore(path).get_revision(tenant_id="t1",project_id="p",revision_id="r1")[0]==b"snapshot"

def test_sync_stale_head_is_conflict_without_partial_revision(tmp_path):
    store=SQLiteSyncStore(tmp_path/"sync.db")
    store.commit(batch(),tenant_id="t",committed_revision_id="r1")
    losing=batch(batch_id="b2",base_revision_id=None)
    assert store.commit(losing,tenant_id="t",committed_revision_id="r2")==SyncConflict("p","b2",None,"r1")
    assert store.get_revision(tenant_id="t",project_id="p",revision_id="r2") is None
