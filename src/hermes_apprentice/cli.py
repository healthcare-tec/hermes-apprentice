"""Interface de linha de comando do hermes-apprentice.

    python -m hermes_apprentice probe      # versão e configuração do Hermes
    python -m hermes_apprentice collect    # coleta eventos (somente observação)
    python -m hermes_apprentice detect     # roda os detectores
    python -m hermes_apprentice scenarios  # lista/valida os cenários
    python -m hermes_apprentice bench      # roda o Qwen3-0.6B na bancada
    python -m hermes_apprentice report     # monta o relatório
    python -m hermes_apprentice all        # tudo acima, do zero

Sem dependências obrigatórias além da biblioteca padrão para ``probe``,
``collect``, ``detect``, ``scenarios`` e ``report``. ``bench`` exige
``torch`` + ``transformers``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from .collector.pipeline import CollectorPipeline, collect_from, summarize
from .detectors.engine import detect_directory
from .probe import probe_environment
from .schema import dump_ndjson

DEFAULT_PATHS = {
    "probe": "artifacts/hermes-environment.json",
    "detection": "artifacts/deteccao.json",
    "benchmark": "artifacts/benchmark-qwen3-0.6b.json",
    "events": "artifacts/eventos.ndjson",
    "report": "reports/relatorio-001-padroes-e-loops.md",
    "scenarios": "scenarios",
}


def _write_json(path: str | Path, data: Dict[str, Any]) -> str:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(out)


def _echo(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------
def cmd_probe(args: argparse.Namespace) -> int:
    report = probe_environment(
        args.hermes_home,
        hermes_bin=args.hermes_bin,
        invoke_cli=not args.no_cli,
        network=not args.offline,
    )
    path = _write_json(args.out, report)
    cli = (report.get("installation") or {}).get("cli") or {}
    _echo(f"[probe] relatório gravado em {path}")
    _echo(f"[probe] HERMES_HOME: {(report.get('hermes_home') or {}).get('path')} "
          f"(regra: {(report.get('hermes_home') or {}).get('source')})")
    _echo(f"[probe] versão do CLI: {cli.get('version_raw') or 'não coletada'}")
    _echo(f"[probe] pacote: {((report.get('installation') or {}).get('pip_metadata') or {}).get('version')}")
    for note in report.get("notes") or []:
        _echo(f"[probe] aviso: {note}")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    pipeline = CollectorPipeline(
        hermes_home=args.hermes_home,
        db_path=args.db,
        logs_dir=args.logs_dir,
        spool_dir=args.spool,
        fixture_path=args.fixture,
        limit=args.limit,
    )
    events = pipeline.run(base_url=args.base_url, run_id=args.run_id, api_key=args.api_key)
    count = dump_ndjson(iter(events), args.out)
    _echo(f"[collect] {count} eventos gravados em {args.out}")
    _echo(f"[collect] fontes usadas: {pipeline.sources_used or ['nenhuma']}")
    _echo(f"[collect] resumo: {json.dumps(summarize(events), ensure_ascii=False)}")
    if not events:
        _echo("[collect] nada coletado — verifique o HERMES_HOME ou use --fixture.")
    return 0 if events or args.allow_empty else 1


def cmd_detect(args: argparse.Namespace) -> int:
    target = Path(args.input)
    if target.is_dir():
        result = detect_directory(target)
    else:
        from .detectors.engine import detect
        from .schema import load_ndjson

        events = list(load_ndjson(str(target)))
        result = detect(events).to_dict()
    path = _write_json(args.out, result)
    summary = result.get("summary") or {}
    _echo(f"[detect] resultado gravado em {path}")
    _echo(f"[detect] {summary.get('total_findings', 0)} achados | "
          f"loops={summary.get('loops', 0)} falhas={summary.get('failures', 0)} | "
          f"pior severidade={summary.get('worst_severity')}")
    for rule, count in (summary.get("rules") or {}).items():
        _echo(f"[detect]   {rule}: {count}")
    if args.fail_on_findings and summary.get("total_findings"):
        return 1
    return 0


def cmd_scenarios(args: argparse.Namespace) -> int:
    root = Path(args.scenarios)
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        _echo("[scenarios] manifesto ausente; rode `python scripts/make_scenarios.py`")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if args.validate:
        import subprocess

        subprocess.run([sys.executable, str(Path("scripts/make_scenarios.py")), "--out", str(root)], check=False)
        _echo("[scenarios] cenários regenerados e validados contra o manifesto")
        return 0
    _echo(f"[scenarios] {manifest['scenario_count']} cenários em {root}")
    for scenario in manifest["scenarios"]:
        _echo(f"[scenarios]   {scenario['id']}: {scenario['event_count']} eventos | "
              f"regras esperadas: {', '.join(scenario['expected_rules'])} | "
              f"correção: {scenario['correction']['strategy']}")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    from .model.bench import run_benchmark

    result = run_benchmark(
        args.scenarios,
        args.out,
        model_id=args.model,
        window_size=args.window_size,
        stride=args.stride,
        max_windows_per_zone=args.max_windows_per_zone,
        max_events=args.max_events,
        device=args.device,
        threads=args.threads,
        local_files_only=args.local_files_only,
        limit=args.limit,
        progress=_echo,
    )
    model = result["model"]
    baseline = result["baseline"]
    _echo("")
    _echo(f"[bench] linha de base: acurácia={baseline['accuracy']} F1 macro={baseline['macro_f1']}")
    _echo(f"[bench] {result['model_id']}: acurácia={model['accuracy']} F1 macro={model['macro_f1']} "
          f"| latência média={model['latency_s']['mean']}s | "
          f"{model['throughput_tokens_per_second']['mean']} tok/s")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from .report.build import build_report

    def load(path: str) -> Dict[str, Any]:
        file = Path(path)
        if not file.exists():
            _echo(f"[report] artefato ausente: {path}")
            return {}
        return json.loads(file.read_text(encoding="utf-8"))

    # O manifesto dos cenários alimenta a seção descritiva do relatório. Lido
    # por padrão para que o relatório nunca saia com uma seção faltando.
    scenarios_meta = args.scenarios_meta
    if scenarios_meta is None:
        manifest = Path(args.scenarios) / "manifest.json"
        if manifest.exists():
            scenarios_meta = json.loads(manifest.read_text(encoding="utf-8")).get("scenarios", [])

    path = build_report(
        load(args.probe),
        load(args.detection),
        load(args.benchmark),
        args.out,
        scenarios=scenarios_meta,
        title=args.title,
    )
    _echo(f"[report] relatório gravado em {path}")
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    """Caminho reprodutível completo: sonda → cenários → detecção → bancada → relatório."""
    import subprocess

    root = Path(args.scenarios)
    if args.regenerate_scenarios:
        _echo("[all] 1/5 regenerando cenários")
        subprocess.run([sys.executable, "scripts/make_scenarios.py", "--out", str(root)], check=True)

    _echo("[all] 2/5 sondando o ambiente do Hermes")
    cmd_probe(argparse.Namespace(
        hermes_home=args.hermes_home, hermes_bin=args.hermes_bin, no_cli=args.no_cli,
        offline=args.offline, out=args.probe,
    ))

    _echo("[all] 3/5 rodando os detectores nos cenários")
    cmd_detect(argparse.Namespace(input=str(root), out=args.detection, fail_on_findings=False))

    if args.skip_bench:
        _echo("[all] 4/5 bancada pulada (--skip-bench)")
    else:
        _echo("[all] 4/5 rodando a bancada do modelo local")
        cmd_bench(argparse.Namespace(
            scenarios=str(root), out=args.benchmark, model=args.model, window_size=args.window_size,
            stride=args.stride, max_windows_per_zone=args.max_windows_per_zone, max_events=args.max_events,
            device=args.device, threads=args.threads, local_files_only=args.local_files_only, limit=args.limit,
        ))

    scenarios_meta: List[Dict[str, Any]] = []
    manifest = root / "manifest.json"
    if manifest.exists():
        scenarios_meta = json.loads(manifest.read_text(encoding="utf-8")).get("scenarios", [])

    _echo("[all] 5/5 montando o relatório")
    cmd_report(argparse.Namespace(
        probe=args.probe, detection=args.detection, benchmark=args.benchmark,
        out=args.report, scenarios_meta=scenarios_meta, title=args.title, scenarios=str(root),
    ))
    _echo("[all] concluído")
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-apprentice",
        description="Aprendiz de observação para o harness Hermes Agent (somente leitura).",
    )
    parser.add_argument("--version", action="version", version=f"hermes-apprentice {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # probe
    p = sub.add_parser("probe", help="identifica versão e configuração do Hermes")
    p.add_argument("--hermes-home")
    p.add_argument("--hermes-bin")
    p.add_argument("--no-cli", action="store_true", help="não executa `hermes --version`")
    p.add_argument("--offline", action="store_true", help="não consulta PyPI/GitHub")
    p.add_argument("--out", default=DEFAULT_PATHS["probe"])
    p.set_defaults(func=cmd_probe)

    # collect
    c = sub.add_parser("collect", help="coleta eventos em modo somente observação")
    c.add_argument("--source", choices=["state_db", "logs", "spool", "runs_api", "fixture", "all"], default="all")
    c.add_argument("--hermes-home")
    c.add_argument("--db")
    c.add_argument("--logs-dir")
    c.add_argument("--spool")
    c.add_argument("--fixture")
    c.add_argument("--base-url")
    c.add_argument("--run-id")
    c.add_argument("--api-key")
    c.add_argument("--limit", type=int, default=5000)
    c.add_argument("--out", default=DEFAULT_PATHS["events"])
    c.add_argument("--allow-empty", action="store_true")
    c.set_defaults(func=cmd_collect)

    # detect
    d = sub.add_parser("detect", help="detecta padrões e loops")
    d.add_argument("--input", required=True, help="arquivo NDJSON ou diretório de cenários")
    d.add_argument("--out", default=DEFAULT_PATHS["detection"])
    d.add_argument("--fail-on-findings", action="store_true")
    d.set_defaults(func=cmd_detect)

    # scenarios
    s = sub.add_parser("scenarios", help="lista ou valida os cenários reproduzíveis")
    s.add_argument("--scenarios", default=DEFAULT_PATHS["scenarios"])
    s.add_argument("--validate", action="store_true")
    s.set_defaults(func=cmd_scenarios)

    # bench
    b = sub.add_parser("bench", help="roda o modelo local na bancada de cenários")
    b.add_argument("--scenarios", default=DEFAULT_PATHS["scenarios"])
    b.add_argument("--model", default="Qwen/Qwen3-0.6B")
    b.add_argument("--window-size", type=int, default=12)
    b.add_argument("--stride", type=int, default=6)
    b.add_argument("--max-windows-per-zone", type=int, default=3)
    b.add_argument("--max-events", type=int, default=16)
    b.add_argument("--device", default="cpu")
    b.add_argument("--threads", type=int, default=None)
    b.add_argument("--local-files-only", action="store_true")
    b.add_argument("--limit", type=int, default=None)
    b.add_argument("--out", default=DEFAULT_PATHS["benchmark"])
    b.set_defaults(func=cmd_bench)

    # report
    r = sub.add_parser("report", help="monta o relatório em Markdown")
    r.add_argument("--probe", default=DEFAULT_PATHS["probe"])
    r.add_argument("--detection", default=DEFAULT_PATHS["detection"])
    r.add_argument("--benchmark", default=DEFAULT_PATHS["benchmark"])
    r.add_argument("--out", default=DEFAULT_PATHS["report"])
    r.add_argument("--title", default="Relatório nº 1 — Padrões e loops detectados")
    r.add_argument("--scenarios-meta", type=json.loads, default=None)
    r.add_argument("--scenarios", default=DEFAULT_PATHS["scenarios"])
    r.set_defaults(func=cmd_report)

    # all
    a = sub.add_parser("all", help="executa o pipeline completo")
    a.add_argument("--scenarios", default=DEFAULT_PATHS["scenarios"])
    a.add_argument("--regenerate-scenarios", action="store_true", default=True)
    a.add_argument("--hermes-home")
    a.add_argument("--hermes-bin")
    a.add_argument("--no-cli", action="store_true")
    a.add_argument("--offline", action="store_true")
    a.add_argument("--skip-bench", action="store_true")
    a.add_argument("--model", default="Qwen/Qwen3-0.6B")
    a.add_argument("--window-size", type=int, default=12)
    a.add_argument("--stride", type=int, default=6)
    a.add_argument("--max-windows-per-zone", type=int, default=3)
    a.add_argument("--max-events", type=int, default=16)
    a.add_argument("--device", default="cpu")
    a.add_argument("--threads", type=int, default=None)
    a.add_argument("--local-files-only", action="store_true")
    a.add_argument("--limit", type=int, default=None)
    a.add_argument("--probe", default=DEFAULT_PATHS["probe"])
    a.add_argument("--detection", default=DEFAULT_PATHS["detection"])
    a.add_argument("--benchmark", default=DEFAULT_PATHS["benchmark"])
    a.add_argument("--report", default=DEFAULT_PATHS["report"])
    a.add_argument("--title", default="Relatório nº 1 — Padrões e loops detectados")
    a.set_defaults(func=cmd_all)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
