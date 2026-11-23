import pytest

from tests.helpers import NOW

from ocre_core.persistence.codec import InvalidPersistedValue, dumps, loads
from ocre_core.persistence.jobs import JobSnapshot
from ocre_core.jobs import JobState


def test_versioned_allowlisted_codec_round_trips_supported_values():
    value=JobSnapshot("j",JobState.QUEUED,1,NOW,b"opaque")
    encoded=dumps(value)
    assert b'"version":1' in encoded
    assert loads(encoded)==value


def test_codec_rejects_unknown_types_without_dynamic_import():
    with pytest.raises(InvalidPersistedValue):
        loads(b'{"version":1,"value":{"$type":"record","name":"os.system","fields":{}}}')
    with pytest.raises(InvalidPersistedValue):
        loads(b'{"version":99,"value":null}')


def test_codec_rejects_non_object_envelopes_and_non_integer_versions():
    with pytest.raises(InvalidPersistedValue):
        loads('{"version":1,"value":1}')
    for raw in (b"[1,2]", b"null", b"5", b'"text"'):
        with pytest.raises(InvalidPersistedValue):
            loads(raw)
    for raw in (b'{"version":true,"value":1}', b'{"version":1.0,"value":1}', b'{"version":"1","value":1}'):
        with pytest.raises(InvalidPersistedValue):
            loads(raw)


def test_dicts_carrying_type_key_round_trip_as_plain_data():
    record_shaped = {"$type": "record", "name": "JobSnapshot", "fields": {"job_id": "x"}}
    assert loads(dumps(record_shaped)) == record_shaped
    nested = {"a": {"$type": "bytes", "value": "aGk="}, "$type": "dict"}
    assert loads(dumps(nested)) == nested
    assert loads(dumps({"$type": "unknown-tag", "k": 1})) == {"$type": "unknown-tag", "k": 1}


def test_codec_rejects_non_string_dict_keys_and_non_finite_numbers():
    for bad in ({1: "a"}, {True: "b"}, {None: "c"}):
        with pytest.raises(TypeError):
            dumps(bad)
    with pytest.raises(ValueError):
        dumps({"nan": float("nan")})
    with pytest.raises(ValueError):
        dumps(float("inf"))
    with pytest.raises(InvalidPersistedValue):
        loads(b'{"version":1,"value":NaN}')


def test_record_decode_enforces_declared_field_types():
    raw = (b'{"version":1,"value":{"$type":"record","name":"JobSnapshot","fields":{'
           b'"job_id":"j","state":"queued","sequence":1,'
           b'"updated_at":{"$type":"datetime","value":"2026-01-01T00:00:00+00:00"},'
           b'"payload":{"$type":"bytes","value":""},"cancellation_origin":null,"tenant_id":"t"}}}')
    with pytest.raises(InvalidPersistedValue):
        loads(raw)
    boolean_sequence = raw.replace(b'"sequence":1', b'"sequence":true')
    with pytest.raises(InvalidPersistedValue):
        loads(boolean_sequence)
    with pytest.raises(InvalidPersistedValue):
        loads(b'{"version":1,"value":{"$type":"tuple","items":{"a":1}}}')
    with pytest.raises(InvalidPersistedValue):
        loads(b'{"version":1,"value":{"$type":"dict","items":[1]}}')
    # Encode-side symmetry: a record whose fields break the schema is not writable.
    with pytest.raises(TypeError):
        dumps(JobSnapshot("j", "queued", 1, NOW))


def test_field_type_matching_checks_collection_elements():
    from ocre_core.persistence.codec import _matches
    assert _matches({"a": 1}, dict[str, int])
    assert not _matches({"a": "x"}, dict[str, int])
    assert not _matches({1: 1}, dict[str, int])
    assert _matches([1, 2], list[int])
    assert not _matches([1, "x"], list[int])
    assert _matches({1, 2}, set[int])
    assert not _matches({1, "x"}, set[int])
    assert _matches((1, "x"), tuple[int, str])
    assert not _matches((1, 2), tuple[int, str])
    assert _matches((1, 2, 3), tuple[int, ...])
    assert not _matches((1, "x"), tuple[int, ...])
