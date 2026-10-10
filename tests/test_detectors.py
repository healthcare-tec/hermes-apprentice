"""Testes dos detectores contra os cenários reproduzíveis."""

import json
from pathlib import Path

import pytest

from hermes_apprentice.detectors import DetectionEngine, DetectorConfig, detect
from hermes_apprentice.detectors.engine import detect_directory
from hermes_apprentice.model.dataset import build_dataset
from hermes_apprentice.model.evaluate import run_baseline, summarize_baseline
from hermes_apprentice.schema import Event, load_ndjson


def _scenario_ids(scenarios_dir: Path) -> list[str]:
    return sorted(p.name for p in scenarios_dir.iterdir() if p.is_dir() and (p / "scenario.json").exists())


@pytest.mark.parametrize("scenario_id", _scenario_ids(Path(__file__).resolve().parents[1] / "scenarios"))
def test_scenarios_emit_expected_rules(scenario_id, scenarios_dir: Path):
    """Cada cenário precisa ser um exemplo válido do modo de falha que declara."""
    directory = scenarios_dir / scenario_id
    meta = json.loads((directory / "scenario.json").read_text(encoding="utf-8"))
    events = list(load_ndjson(str(directory / meta["events_file"])))
    detected = {f.rule for f in detect(events).findings}
    missing = set(meta["expected_rules"]) - detected
    assert not missing, f"{scenario_id}: regras esperadas ausentes: {sorted(missing)}"


def test_scenario_count_is_five(scenarios_dir: Path):
    assert len(_scenario_ids(scenarios_dir)) == 5


def test_every_scenario_has_a_correction_phase(scenarios_dir: Path):
    for scenario_id in _scenario_ids(scenarios_dir):
        meta = json.loads((scenarios_dir / scenario_id / "scenario.json").read_text(encoding="utf-8"))
        phases = [zone["phase"] for zone in meta["label_zones"]]
        assert meta["correction"]["strategy"], f"{scenario_id} sem estratégia de correção"
        assert "correção" in phases, f"{scenario_id} sem zona de correção rotulada"


def test_healthy_zones_do_not_trigger_rules(scenarios_dir: Path):
    """A zona saudável precisa estar limpa — senão o rótulo do cenário é mentira."""
    for scenario_id in _scenario_ids(scenarios_dir):
        directory = scenarios_dir / scenario_id
        meta = json.loads((directory / "scenario.json").read_text(encoding="utf-8"))
        events = list(load_ndjson(str(directory / meta["events_file"])))
        for zone in meta["label_zones"]:
            if zone["label"] != "normal" or zone["phase"] == "correção":
                continue
            chunk = events[zone["from_event"] : zone["to_event"] + 1]
            findings = detect(chunk).findings
            assert not findings, f"{scenario_id}/{zone['phase']}: achados inesperados {[f.rule for f in findings]}"


def test_baseline_is_perfect_on_labelled_windows(scenarios_dir: Path):
    """A linha de base é o teto por construção; se cair, algo regrediu."""
    windows = build_dataset(scenarios_dir, window_size=12, stride=6, max_windows_per_zone=3)
    assert len(windows) == 30
    summary = summarize_baseline(run_baseline(windows))
    assert summary["accuracy"] == 1.0, summary["confusion_matrix"]


def test_detect_directory_aggregates(scenarios_dir: Path):
    result = detect_directory(scenarios_dir)
    summary = result["summary"]
    assert summary["sessions_analyzed"] == 5
    assert summary["scenarios_matched"] == 5
    assert summary["total_findings"] > 0
    assert summary["loops"] > 0 and summary["failures"] > 0


def test_repetition_requires_threshold():
    config = DetectorConfig(repetition_min=3)
    events = [
        Event(type="tool_call_start", tool="read_file", args_digest="x", session_id="s"),
        Event(type="tool_call_start", tool="read_file", args_digest="x", session_id="s"),
    ]
    assert detect(events, config).findings == []
    events.append(Event(type="tool_call_start", tool="read_file", args_digest="x", session_id="s"))
    assert {f.rule for f in detect(events, config).findings} == {"loop.repetition"}


def test_different_arguments_do_not_trigger_repetition():
    events = [
        Event(type="tool_call_start", tool="read_file", args_digest=f"arg{i}", session_id="s")
        for i in range(6)
    ]
    assert detect(events).findings == []


def test_cycle_requires_varying_arguments():
    same = [
        Event(type="tool_call_start", tool="web_search", args_digest="a", session_id="s"),
        Event(type="tool_call_start", tool="web_extract", args_digest="a", session_id="s"),
    ] * 4
    assert "loop.cycle" not in {f.rule for f in detect(same).findings}


def test_oscillation_needs_a_single_tool():
    events = []
    for index in range(8):
        events.append(
            Event(
                type="tool_call_end",
                tool="terminal" if index % 2 == 0 else "read_file",
                result_digest="d1" if index % 2 == 0 else "d2",
                session_id="s",
            )
        )
    assert "loop.oscillation" not in {f.rule for f in detect(events).findings}


def test_engine_is_deterministic(scenarios_dir: Path):
    directory = scenarios_dir / "s1_loop_read_file_dedup"
    events = list(load_ndjson(str(directory / "events.ndjson")))
    first = [f.rule for f in DetectionEngine().run(events).findings]
    second = [f.rule for f in DetectionEngine().run(events).findings]
    assert first == second