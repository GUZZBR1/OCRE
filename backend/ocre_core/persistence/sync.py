"""Atomic local SQLite adapter for project sync batches."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ocre_core.sync import BatchReceipt, SyncBatch, SyncBatchIdentityConflict, SyncConflict, SyncReplay, SyncReady, evaluate_sync_batch
from .sqlite import PersistenceConflict
from . import codec


@dataclass(frozen=True)
class SyncCommit:
    receipt: BatchReceipt
    revision_snapshot: bytes
    operations: tuple


class SQLiteSyncStore:
    """SQLite-only proof of local transaction/restart semantics."""
    def __init__(self, database):
        self.database=str(database)
        with sqlite3.connect(self.database) as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS sync_heads(tenant TEXT, project TEXT, revision TEXT, PRIMARY KEY(tenant,project));
            CREATE TABLE IF NOT EXISTS sync_receipts(tenant TEXT, project TEXT, batch TEXT, envelope BLOB, receipt BLOB, PRIMARY KEY(tenant,project,batch));
            CREATE TABLE IF NOT EXISTS sync_revisions(tenant TEXT, project TEXT, revision TEXT, batch TEXT, snapshot BLOB, operations BLOB, PRIMARY KEY(tenant,project,revision), UNIQUE(tenant,project,batch));
            """)

    @staticmethod
    def _dump(value): return sqlite3.Binary(codec.dumps(value))
    @staticmethod
    def _load(value): return codec.loads(value) if value is not None else None

    def current_head(self, *, tenant_id, project_id):
        with sqlite3.connect(self.database) as db: row=db.execute("SELECT revision FROM sync_heads WHERE tenant=? AND project=?",(tenant_id,project_id)).fetchone()
        return row[0] if row else None

    def lookup(self, *, tenant_id, project_id, batch_id):
        with sqlite3.connect(self.database) as db: row=db.execute("SELECT receipt FROM sync_receipts WHERE tenant=? AND project=? AND batch=?",(tenant_id,project_id,batch_id)).fetchone()
        return self._load(row[0]) if row else None

    def evaluate(self, batch: SyncBatch, *, tenant_id):
        prior=self.lookup(tenant_id=tenant_id,project_id=batch.project_id,batch_id=batch.batch_id)
        return evaluate_sync_batch(batch,current_head_revision_id=self.current_head(tenant_id=tenant_id,project_id=batch.project_id),prior_receipt=prior)

    def commit(self, batch: SyncBatch, *, tenant_id, committed_revision_id, revision_snapshot=b""):
        """Append immutable revision atomically; tenant is a storage namespace."""
        db=sqlite3.connect(self.database,timeout=15,isolation_level=None)
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("BEGIN IMMEDIATE")
        try:
            row=db.execute("SELECT envelope,receipt FROM sync_receipts WHERE tenant=? AND project=? AND batch=?",(tenant_id,batch.project_id,batch.batch_id)).fetchone()
            if row:
                prior=self._load(row[1])
                if self._load(row[0]) != batch:
                    db.rollback(); return SyncBatchIdentityConflict(batch.project_id,batch.batch_id)
                db.rollback(); return SyncReplay(prior)
            h=db.execute("SELECT revision FROM sync_heads WHERE tenant=? AND project=?",(tenant_id,batch.project_id)).fetchone()
            head=h[0] if h else None
            decision=evaluate_sync_batch(batch,current_head_revision_id=head)
            if isinstance(decision,SyncConflict): db.rollback(); return decision
            if not isinstance(decision,SyncReady): raise RuntimeError("unexpected sync decision")
            receipt=BatchReceipt(batch,committed_revision_id)
            db.execute("INSERT INTO sync_revisions VALUES(?,?,?,?,?,?)",(tenant_id,batch.project_id,committed_revision_id,batch.batch_id,self._dump(revision_snapshot),self._dump(batch.operations)))
            db.execute("INSERT INTO sync_receipts VALUES(?,?,?,?,?)",(tenant_id,batch.project_id,batch.batch_id,self._dump(batch),self._dump(receipt)))
            if h:
                changed=db.execute("UPDATE sync_heads SET revision=? WHERE tenant=? AND project=? AND revision=?",(committed_revision_id,tenant_id,batch.project_id,head)).rowcount
                if changed != 1: raise PersistenceConflict("sync head compare-and-swap lost")
            else: db.execute("INSERT INTO sync_heads VALUES(?,?,?)",(tenant_id,batch.project_id,committed_revision_id))
            db.commit(); return receipt
        except sqlite3.IntegrityError as exc:
            db.rollback(); raise PersistenceConflict("revision identity collision") from exc
        except Exception: db.rollback(); raise
        finally: db.close()

    def get_revision(self, *, tenant_id, project_id, revision_id):
        with sqlite3.connect(self.database) as db: row=db.execute("SELECT snapshot,operations FROM sync_revisions WHERE tenant=? AND project=? AND revision=?",(tenant_id,project_id,revision_id)).fetchone()
        return (self._load(row[0]),self._load(row[1])) if row else None
