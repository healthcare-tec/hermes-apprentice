"""Testes do coletor — inclusive a prova de que a escrita é impossível."""

import sqlite3
from pathlib import Path

import pytest

from hermes_apprentice.collector import logs as logs_source
from hermes_apprentice.collector import runs_api, spool, state_db
from hermes_apprentice.collector.pipeline import CollectorPipeline, collect_from, normalize, summarize
from hermes_apprentice.schema import Event


def test_readonly_connection_refuses_writes(fixture_home: Path):
    result = state_db.assert_readonly(fixture_home / "state.db")
    assert result["write_blocked"] is True
    assert "readonly" in result["error"].lower()


def test_connect_readonly_declares_query_only(fixture_home: Path):
    conn = state_db.connect_readonly(fixture_home / "state.db")
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        conn.close()


def test_collect_from_real_state_db(fixture_home: Path):
    events = collect_from("state_db", hermes_home=str(fixture_home))
    assert events, "o fixture autêntico precisa produzir eventos"
    assert any(e.type == "tool_call_start" for e in events)
    assert all(e.source == "state_db" for e in events)
    assert any(e.session_id == "sess_fixture_collector" for e in events if e.session_id)


def test_collected_events_form_a_detectable_loop(fixture_home: Path):
    """O fixture do Hermes real contém três `read_file` idênticos: é um loop de verdade."""
    from hermes_apprentice.detectors import detect

    events = collect_from("state_db", hermes_home=str(fixture_home))
    rules = {f.rule for f in detect(events).findings}
    assert "loop.repetition" in rules


def test_normalize_dedupes_across_sources():
    duplicated = [
        Event(type="tool_call_start", ts="2026-01-01T00:00:00Z", source="logs", tool="terminal", session_id="s"),
        Event(type="tool_call_start", ts="2026-01-01T00:00:00Z", source="state_db", tool="terminal", session_id="s"),
    ]
    assert len(normalize(duplicated)) == 1


def test_summarize_counts():
    events = [
        Event(type="tool_call_start", tool="read_file", session_id="s1"),
        Event(type="tool_call_end", tool="read_file", session_id="s1"),
        Event(type="run_end", session_id="s2"),
    ]
    summary = summarize(events)
    assert summary["total_events"] == 3
    assert summary["sessions"] == 2
    assert summary["tools"]["read_file"] == 2


def test_logs_classifier_maps_known_signatures():
    assert logs_source.classify_line("2026-10-05 12:00:00 ERROR database is locked")["type"] == "persistence_failure"
    budget = logs_source.classify_line("turn_exit_reason=max_iterations_reached(60/60)")
    assert budget["type"] == "budget_exhausted"
    assert budget["error_type"] == "budget_exhausted"
    storm = logs_source.classify_line("api_request_error status=429 retryable=true")
    assert storm["type"] == "api_error"
    assert storm["error_type"] == "provider_rate_limit"


def test_logs_tail_respects_offset(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text("linha 1\nlinha 2\n", encoding="utf-8")
    lines, offset = logs_source.tail_file(log, offset=0)
    assert len(lines) == 2
    log.write_text("linha 1\nlinha 2\nlinha 3\n", encoding="utf-8")
    lines, new_offset = logs_source.tail_file(log, offset=offset)
    assert lines == ["linha 3"]
    assert new_offset > offset


def test_runs_api_parses_sse_stream():
    stream = iter(
        [
            ": keep-alive",
            "event: run.started",
            'data: {"type":"run.started","session_id":"s1","model":"nous/qwen3-0.6b","ts":"2026-01-01T00:00:00Z"}',
            'data: {"type":"tool.started","call_id":"c1","tool":"read_file","args":{"path":"a.md"}}',
            'data: {"type":"tool.completed","call_id":"c1","tool":"read_file","status":"ok","duration_ms":12,"result":"conteudo"}',
            'data: {"type":"run.completed","status":"completed","completed":true}',
            "data: [DONE]",
        ]
    )
    events = list(runs_api.parse_sse_lines(stream, run_id="run_1"))
    types = [e.type for e in events]
    assert types == ["run_start", "tool_call_start", "tool_call_end", "run_end"]
    assert events[1].args_digest and events[1].args_preview
    assert events[2].result_digest and events[2].duration_ms == 12
    assert events[3].status == "completed"
    assert all(e.source == "runs_api" for e in events)


def test_runs_api_maps_failed_run():
    event = runs_api.normalize_run_event(
        "run.failed", {"status": "failed", "error": "8 tentativas esgotadas", "turn_exit_reason": "max_iterations_reached(60/60)"}
    )
    assert event.type == "run_end"
    assert event.status == "failed"
    assert event.meta["turn_exit_reason"] == "max_iterations_reached(60/60)"


def test_spool_ignores_foreign_records_and_maps_hooks(tmp_path):
    (tmp_path / "spool.ndjson").write_text(
        "\n".join(
            [
                '{"hook_event_name":"pre_tool_call","tool_name":"terminal","tool_input":{"command":"ls"},"observer":"outro-projeto"}',
                '{"hook_event_name":"pre_tool_call","tool_name":"terminal","tool_input":{"command":"ls"},"observer":"hermes-apprentice","session_id":"s1"}',
                '{"hook_event_name":"api_request_error","error":"status=503","observer":"hermes-apprentice","session_id":"s1"}',
            ]
        ),
        encoding="utf-8",
    )
    events = list(spool.iter_events(tmp_path))
    assert len(events) == 2
    assert events[0].type == "tool_call_start" and events[0].args_digest
    assert events[1].type == "api_error" and events[1].status == "error"


def test_pipeline_reports_sources_used(fixture_home: Path):
    pipeline = CollectorPipeline(hermes_home=str(fixture_home))
    events = pipeline.run()
    assert "state_db" in pipeline.sources_used
    assert events


def test_missing_home_does_not_crash(tmp_path):
    pipeline = CollectorPipeline(hermes_home=str(tmp_path / "inexistente"))
    assert pipeline.run() == []
    assert collect_from("state_db", hermes_home=str(tmp_path / "inexistente")) == []


def test_immutable_read_creates_no_sidecar(fixture_home: Path, tmp_path: Path):
    """Ler com ``immutable=1`` não pode criar `-shm`/`-wal` ao lado do banco."""
    import shutil

    target = tmp_path / "state.db"
    shutil.copy2(fixture_home / "state.db", target)
    before = sorted(p.name for p in tmp_path.iterdir())

    events = list(state_db.iter_events(target, immutable=True))
    assert events

    after = sorted(p.name for p in tmp_path.iterdir())
    assert after == before == ["state.db"]
    assert state_db.sidecar_paths(target) == []


def test_immutable_still_refuses_writes(fixture_home: Path):
    """A garantia de somente leitura não pode depender de ``PRAGMA query_only``."""
    result = state_db.assert_readonly(fixture_home / "state.db", immutable=True)
    assert result["write_blocked"] is True


def test_reader_reports_sidecars_instead_of_hiding_them(fixture_home: Path):
    """O efeito colateral do SQLite é reportado, e o arquivo não muda.

    O que importa não é *se* a biblioteca cria sidecars, e sim que: (a) o
    ``state.db`` em si fique byte a byte idêntico, e (b) qualquer sidecar criado
    seja visível para quem audita, via ``sidecar_paths``.
    """
    import hashlib
    import shutil

    target = fixture_home / "state.db"
    before_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    conn = state_db.connect_readonly(target)
    try:
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
    finally:
        conn.close()

    after_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    assert after_hash == before_hash, "o modo=ro alterou o arquivo do banco"

    # Qualquer sidecar presente precisa ser reportado — nada é escondido.
    reported = state_db.sidecar_paths(target)
    for suffix in ("-shm", "-wal"):
        assert (str(target) + suffix in reported) == Path(str(target) + suffix).exists()
    if mode == "wal":
        assert reported, "banco WAL lido em mode=ro deveria expor sidecars"


def test_pipeline_immutable_flag_is_forwarded(tmp_path, fixture_home: Path):
    import shutil

    home = tmp_path / "home"
    home.mkdir()
    shutil.copy2(fixture_home / "state.db", home / "state.db")
    pipeline = CollectorPipeline(hermes_home=str(home), immutable=True)
    assert pipeline.run()
    assert (home / "state.db").exists()
    assert not (home / "state.db-shm").exists()
