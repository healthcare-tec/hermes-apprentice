"""Testes do esquema normalizado."""

from hermes_apprentice.schema import Event, digest, dump_ndjson, load_ndjson


def test_roundtrip(tmp_path):
    events = [
        Event(type="tool_call_start", tool="read_file", args_digest="abc", source="fixture"),
        Event(type="run_end", status="failed", error_type="provider_rate_limit", source="runs_api"),
    ]
    path = tmp_path / "e.ndjson"
    assert dump_ndjson(iter(events), str(path)) == 2
    loaded = list(load_ndjson(str(path)))
    assert [e.type for e in loaded] == ["tool_call_start", "run_end"]
    assert loaded[1].status == "failed"


def test_unknown_type_is_coerced():
    event = Event(type="tipo_que_nao_existe")
    assert event.type == "unknown"
    assert Event(type="tool_call_start", source="fonte_invalida").source == "fixture"


def test_extra_fields_go_to_meta():
    event = Event.from_dict({"type": "tool_call_start", "campo_extra": 42})
    assert event.meta["campo_extra"] == 42


def test_digest_is_stable_and_order_insensitive():
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    assert digest({"a": 1}) != digest({"a": 2})


def test_tool_signature():
    event = Event(type="tool_call_start", tool="terminal", args_digest="deadbeef")
    assert event.tool_signature == "terminal:deadbeef"
    assert Event(type="tool_call_start").tool_signature is None


def test_to_dict_omits_empty_fields():
    data = Event(type="tool_call_start", tool="read_file").to_dict()
    assert "error_message" not in data
    assert data["tool"] == "read_file"