import pytest

from ocre_core.persistence.codec import InvalidPersistedValue, dumps, loads
from ocre_core.persistence.jobs import JobSnapshot
from ocre_core.jobs import JobState
from datetime import datetime, timezone


def test_versioned_allowlisted_codec_round_trips_supported_values():
    value=JobSnapshot("j",JobState.QUEUED,1,datetime(2026,1,1,tzinfo=timezone.utc),b"opaque")
    encoded=dumps(value)
    assert b'"version":1' in encoded
    assert loads(encoded)==value


def test_codec_rejects_unknown_types_without_dynamic_import():
    with pytest.raises(InvalidPersistedValue):
        loads(b'{"version":1,"value":{"$type":"record","name":"os.system","fields":{}}}')
    with pytest.raises(InvalidPersistedValue):
        loads(b'{"version":99,"value":null}')
