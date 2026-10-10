"""Motor de detecção: executa todas as regras e consolida o veredito por sessão."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..schema import Event
from .base import DetectorConfig, SEVERITY_ORDER, Finding, worst_severity
from . import failures as failure_rules
from . import loops as loop_rules


@dataclass
class DetectionResult:
    """Resultado consolidado de uma execução de detecção."""

    findings: List[Finding] = field(default_factory=list)
    sessions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    stats: Dict[str, Any] = field(default_factory=dict)

    @property
    def loops(self) -> List[Finding]:
        return [f for f in self.findings if f.category == "loop"]

    @property
    def failures(self) -> List[Finding]:
        return [f for f in self.findings if f.category == "failure"]

    def labels(self) -> List[str]:
        return sorted({f.rule for f in self.findings})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": 1,
            "summary": {
                "total_findings": len(self.findings),
                "loops": len(self.loops),
                "failures": len(self.failures),
                "rules": self.rule_counts(),
                "sessions_analyzed": len(self.sessions),
                "worst_severity": worst_severity(self.findings),
            },
            "sessions": self.sessions,
            "stats": self.stats,
            "findings": [f.to_dict() for f in self.findings],
        }

    def rule_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for finding in self.findings:
            counts[finding.rule] = counts.get(finding.rule, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


DEFAULT_RULES = [
    loop_rules.detect_repetition,
    loop_rules.detect_no_progress,
    loop_rules.detect_cycle,
    loop_rules.detect_oscillation,
    failure_rules.detect_budget_exhausted,
    failure_rules.detect_persistence_locked,
    failure_rules.detect_retry_storm,
    failure_rules.detect_run_failures,
]


class DetectionEngine:
    """Executa regras e produz achados + veredito por sessão."""

    def __init__(self, config: Optional[DetectorConfig] = None, rules: Optional[Sequence[Any]] = None):
        self.config = config or DetectorConfig()
        self.rules = list(rules) if rules is not None else list(DEFAULT_RULES)

    def run(self, events: Sequence[Event]) -> DetectionResult:
        findings: List[Finding] = []
        for rule in self.rules:
            findings.extend(rule(events, self.config))

        # Remove duplicatas: regras diferentes podem apontar o mesmo trecho.
        unique: Dict[str, Finding] = {}
        for finding in findings:
            key = finding.signature
            if key not in unique or SEVERITY_ORDER.get(finding.severity, 0) > SEVERITY_ORDER.get(
                unique[key].severity, 0
            ):
                unique[key] = finding
        ordered = sorted(
            unique.values(),
            key=lambda f: (-SEVERITY_ORDER.get(f.severity, 0), f.start_ts or ""),
        )

        sessions: Dict[str, Dict[str, Any]] = {}
        for finding in ordered:
            session = sessions.setdefault(
                finding.session_id or "<sem-sessao>",
                {"findings": [], "verdict": "normal", "rules": []},
            )
            session["findings"].append(finding.to_dict())
            session["rules"].append(finding.rule)
        for session in sessions.values():
            session["rules"] = sorted(set(session["rules"]))
            if any(rule.startswith("failure.") for rule in session["rules"]):
                session["verdict"] = "failure"
            elif any(rule.startswith("loop.") for rule in session["rules"]):
                session["verdict"] = "loop"

        return DetectionResult(
            findings=ordered,
            sessions=sessions,
            stats={
                "events_analyzed": len(events),
                "tool_calls": sum(1 for e in events if e.type == "tool_call_start"),
                "config": self.config.__dict__,
            },
        )


def detect(events: Sequence[Event], config: Optional[DetectorConfig] = None) -> DetectionResult:
    """Atalho funcional."""
    return DetectionEngine(config=config).run(events)


def detect_directory(
    scenarios_dir: Any,
    config: Optional[DetectorConfig] = None,
) -> Dict[str, Any]:
    """Roda a detecção em cada cenário de um diretório e agrega os achados.

    A detecção é feita **por sessão** (nunca sobre a concatenação), porque um
    achado só faz sentido dentro da sessão em que o loop aconteceu.
    """
    import json as _json
    from pathlib import Path

    from ..schema import load_ndjson

    directory = Path(scenarios_dir)
    engine = DetectionEngine(config=config)
    findings: List[Finding] = []
    per_scenario: Dict[str, Any] = {}
    total_events = 0

    for scenario_dir in sorted(p for p in directory.iterdir() if p.is_dir()):
        scenario_file = scenario_dir / "scenario.json"
        events_file = scenario_dir / "events.ndjson"
        if not scenario_file.exists() or not events_file.exists():
            continue
        meta = _json.loads(scenario_file.read_text(encoding="utf-8"))
        events = list(load_ndjson(str(events_file)))
        total_events += len(events)
        result = engine.run(events)
        findings.extend(result.findings)
        detected_rules = sorted({f.rule for f in result.findings})
        expected = sorted(meta.get("expected_rules", []))
        verdicts = [session.get("verdict", "normal") for session in result.sessions.values()]
        per_scenario[meta.get("id", scenario_dir.name)] = {
            "title": meta.get("title"),
            "expected_rules": expected,
            "detected_rules": detected_rules,
            "match": set(expected) <= set(detected_rules),
            "verdict": verdicts[0] if verdicts else "normal",
            "expected_verdict": meta.get("expected_verdict"),
            "findings": len(result.findings),
            "events": len(events),
        }

    rule_counts: Dict[str, int] = {}
    for finding in findings:
        rule_counts[finding.rule] = rule_counts.get(finding.rule, 0) + 1

    return {
        "schema_version": 1,
        "summary": {
            "total_findings": len(findings),
            "loops": sum(1 for f in findings if f.category == "loop"),
            "failures": sum(1 for f in findings if f.category == "failure"),
            "rules": dict(sorted(rule_counts.items(), key=lambda kv: -kv[1])),
            "sessions_analyzed": len(per_scenario),
            "worst_severity": worst_severity(findings),
            "events_analyzed": total_events,
            "scenarios_matched": sum(1 for s in per_scenario.values() if s["match"]),
        },
        "scenarios": per_scenario,
        "findings": [f.to_dict() for f in findings],
        "stats": {"config": engine.config.__dict__},
    }


def verdict_for(result: DetectionResult, session_id: str) -> str:
    """Veredito de uma sessão: ``normal`` | ``loop`` | ``failure``."""
    return (result.sessions.get(session_id) or {}).get("verdict", "normal")
