"""Pipeline de coleta: une as fontes, ordena, deduplica e grava NDJSON.

Ordem de preferência das fontes (da mais estável para a mais heurística):

1. ``runs_api``  — contrato publicado de eventos (SSE).
2. ``spool``     — hook de observação, opt-in explícito do usuário.
3. ``state_db``  — estado persistido (fonte da verdade *ex post facto*).
4. ``logs``      — texto humano; útil para o que não chega às outras fontes.
5. ``fixture``   — arquivo NDJSON já normalizado (usado nos cenários e testes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from ..schema import Event, dump_ndjson, load_ndjson
from . import logs as logs_source
from . import runs_api as runs_api_source
from . import spool as spool_source
from . import state_db as state_db_source

SOURCE_PREFERENCE = ["runs_api", "spool", "state_db", "logs", "fixture"]


@dataclass
class CollectorPipeline:
    """Coleta eventos de uma ou mais fontes, normaliza e entrega uma lista ordenada."""

    hermes_home: Optional[str] = None
    db_path: Optional[str] = None
    logs_dir: Optional[str] = None
    spool_dir: Optional[str] = None
    run_events_path: Optional[str] = None
    fixture_path: Optional[str] = None
    since_row_id: int = 0
    limit: int = 5000
    sources_used: List[str] = field(default_factory=list)

    # -- fontes individuais ------------------------------------------------
    def from_state_db(self) -> List[Event]:
        path = Path(self.db_path or Path(self.hermes_home or "~/.hermes").expanduser() / "state.db")
        if not path.exists():
            return []
        self.sources_used.append("state_db")
        return list(state_db_source.iter_events(path, since_row_id=self.since_row_id, limit=self.limit))

    def from_logs(self) -> List[Event]:
        directory = Path(self.logs_dir or Path(self.hermes_home or "~/.hermes").expanduser() / "logs")
        if not directory.is_dir():
            return []
        self.sources_used.append("logs")
        return list(logs_source.iter_events(directory))

    def from_spool(self) -> List[Event]:
        if not self.spool_dir:
            return []
        directory = Path(self.spool_dir).expanduser()
        if not directory.is_dir():
            return []
        self.sources_used.append("spool")
        return list(spool_source.iter_events(directory))

    def from_runs_api(self, base_url: str, run_id: str, *, api_key: Optional[str] = None) -> List[Event]:
        self.sources_used.append("runs_api")
        return list(runs_api_source.iter_events(base_url, run_id, api_key=api_key))

    def from_fixture(self) -> List[Event]:
        if not self.fixture_path:
            return []
        self.sources_used.append("fixture")
        return list(load_ndjson(self.fixture_path))

    # -- pipeline ----------------------------------------------------------
    def run(self, *, base_url: Optional[str] = None, run_id: Optional[str] = None, api_key: Optional[str] = None) -> List[Event]:
        collected: List[Event] = []
        if base_url and run_id:
            collected.extend(self.from_runs_api(base_url, run_id, api_key=api_key))
        collected.extend(self.from_spool())
        collected.extend(self.from_state_db())
        collected.extend(self.from_logs())
        collected.extend(self.from_fixture())
        return normalize(collected)

    def write(self, events: Iterable[Event], out_path: str | Path) -> int:
        return dump_ndjson(iter(events), str(out_path))


def normalize(events: Sequence[Event]) -> List[Event]:
    """Ordena por timestamp (estável) e remove duplicatas.

    Duplicatas surgem naturalmente quando a mesma ação é vista pelo banco **e**
    por um log. A chave considera tipo, sessão, instante, ferramenta e id de
    chamada — dois eventos genuinamente distintos nunca colidem nesses cinco
    campos dimensionais.
    """
    ordered = sorted(
        enumerate(events),
        key=lambda pair: (pair[1].ts or "", SOURCE_PREFERENCE.index(pair[1].source) if pair[1].source in SOURCE_PREFERENCE else 99, pair[0]),
    )
    seen = set()
    out: List[Event] = []
    for _, event in ordered:
        key = (
            event.type,
            event.session_id,
            event.ts,
            event.tool,
            event.tool_call_id,
            event.seq if event.source == "state_db" else None,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(event)
    return out


def collect_from(source: str, **kwargs: Any) -> List[Event]:
    """Atalho: coleta de uma única fonte por nome."""
    pipeline = CollectorPipeline(**{k: v for k, v in kwargs.items() if k in CollectorPipeline.__dataclass_fields__})
    if source == "state_db":
        return normalize(pipeline.from_state_db())
    if source == "logs":
        return normalize(pipeline.from_logs())
    if source == "spool":
        return normalize(pipeline.from_spool())
    if source == "fixture":
        return normalize(pipeline.from_fixture())
    if source == "runs_api":
        return normalize(pipeline.from_runs_api(kwargs["base_url"], kwargs["run_id"], api_key=kwargs.get("api_key")))
    if source == "all":
        return pipeline.run(base_url=kwargs.get("base_url"), run_id=kwargs.get("run_id"), api_key=kwargs.get("api_key"))
    raise ValueError(f"fonte desconhecida: {source}")


def summarize(events: Sequence[Event]) -> Dict[str, Any]:
    """Resumo agregado usado em relatórios e no CLI."""
    by_type: Dict[str, int] = {}
    by_source: Dict[str, int] = {}
    sessions = set()
    tools: Dict[str, int] = {}
    for event in events:
        by_type[event.type] = by_type.get(event.type, 0) + 1
        by_source[event.source] = by_source.get(event.source, 0) + 1
        if event.session_id:
            sessions.add(event.session_id)
        if event.tool:
            tools[event.tool] = tools.get(event.tool, 0) + 1
    return {
        "total_events": len(events),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "by_source": dict(sorted(by_source.items(), key=lambda kv: -kv[1])),
        "sessions": len(sessions),
        "tools": dict(sorted(tools.items(), key=lambda kv: -kv[1])),
        "first_ts": events[0].ts if events else None,
        "last_ts": events[-1].ts if events else None,
    }