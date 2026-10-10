"""Testes do gerador de relatório e do conjunto de janelas rotuladas."""

import json
from pathlib import Path

from hermes_apprentice.model.dataset import build_dataset, dataset_summary
from hermes_apprentice.report.build import build_report, render_markdown

FAKE_PROBE = {
    "hermes_home": {"path": "/tmp/home", "source": "explicit", "exists": True},
    "installation": {"cli": {"version_raw": "Hermes Agent v0.19.0 (2026.7.20)"}, "pip_metadata": {"version": "0.19.0"}},
    "upstream": {"github_latest_release": {"tag": "v0.21.6"}},
    "state_db": {"exists": True},
    "profiles": [],
    "notes": ["Divergência de versões."],
}

FAKE_DETECTION = {
    "summary": {
        "total_findings": 12,
        "loops": 5,
        "failures": 7,
        "sessions_analyzed": 5,
        "rules": {"loop.repetition": 2, "failure.retry_storm": 2},
    }
}

FAKE_BENCH = {
    "dataset": {"total_windows": 30, "by_label": {"normal": 15, "loop": 9, "failure": 6}, "window_size": 12},
    "hardware": {"cpu_cores": 6, "ram_gb": 7.8, "device": "cpu", "dtype": "float32", "load_seconds": 5.7},
    "baseline": {
        "accuracy": 1.0, "macro_f1": 1.0,
        "per_class": {k: {"f1": 1.0} for k in ("normal", "loop", "failure")},
        "latency_s": {"mean": 0.0001},
    },
    "model": {
        "accuracy": 0.7, "macro_f1": 0.552, "parse_failures": 8,
        "per_class": {"normal": {"f1": 0.9}, "loop": {"f1": 0.4}, "failure": {"f1": 1.0}},
        "confusion_matrix": [[15, 0, 0], [5, 4, 0], [0, 4, 2]],
        "labels": ["normal", "loop", "failure"],
        "latency_s": {"mean": 8.53, "median": 8.5, "p95": 10.1},
        "throughput_tokens_per_second": {"mean": 5.3, "min": 4.7, "max": 6.2},
    },
    "model_windows": [{"latency_s": 8.0}, {"latency_s": 9.0}],
    "errors": [{"scenario_id": "s1", "truth": "loop", "predicted": "normal", "loop_type": "none", "reason": "parece normal"}],
}


def test_dataset_is_balanced_and_inside_zones(scenarios_dir: Path):
    windows = build_dataset(scenarios_dir, window_size=12, stride=6, max_windows_per_zone=3)
    summary = dataset_summary(windows)
    assert summary["total_windows"] == 30
    assert summary["by_label"] == {"failure": 6, "loop": 9, "normal": 15}
    for window in windows:
        assert window.size == 12
        zones = json.loads(
            (scenarios_dir / window.scenario_id / "scenario.json").read_text(encoding="utf-8")
        )["label_zones"]
        # A janela precisa estar inteiramente dentro de uma única zona, e o
        # rótulo da janela precisa ser o rótulo dessa zona — nunca de outra.
        containing = [z for z in zones if z["from_event"] <= window.start and window.end <= z["to_event"]]
        assert len(containing) == 1, f"{window.scenario_id}: janela {window.start}-{window.end} fora de zona única"
        assert containing[0]["label"] == window.label


def test_render_markdown_contains_key_sections():
    markdown = render_markdown(FAKE_PROBE, FAKE_DETECTION, FAKE_BENCH)
    for expected in [
        "# Relatório nº 1",
        "Resumo executivo",
        "Versão e configuração do Hermes identificadas",
        "somente observação",
        "Padrões e loops detectados",
        "Desempenho inicial do Qwen3-0.6B",
        "Próximos passos",
        "0.7",
        "0.552",
    ]:
        assert expected in markdown, expected


def test_build_report_writes_file_and_charts(tmp_path: Path):
    out = tmp_path / "relatorio.md"
    path = build_report(FAKE_PROBE, FAKE_DETECTION, FAKE_BENCH, out, charts_dir=tmp_path / "assets")
    content = Path(path).read_text(encoding="utf-8")
    assert "Relatório nº 1" in content
    charts = list((tmp_path / "assets").glob("*.png"))
    # matplotlib pode não estar instalado; se estiver, os gráficos precisam sair.
    try:
        import matplotlib  # noqa: F401

        assert charts, "esperava gráficos PNG"
        assert "![" in content
    except ImportError:
        assert not charts
