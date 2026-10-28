"""OCRE's language- and storage-independent backend domain core."""

from .jobs import JobState, JobTransition, transition_job
from .sync import SyncConflict, check_sync_base

__all__ = [
    "JobState",
    "JobTransition",
    "SyncConflict",
    "check_sync_base",
    "transition_job",
]
