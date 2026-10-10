"""Esquema normalizado de eventos.

Qualquer que seja a origem (SQLite ``state.db`` do Hermes, arquivos de log, o
stream SSE da Runs API, um diretório de spool alimentado por hooks ou um
fixture gravado), todo evento é normalizado para este formato único antes de
chegar a um detector.

``SCHEMA_VERSION`` é incrementado sempre que um campo é adicionado ou muda de
significado. Consumidores devem comparar a versão antes de interpretar campos.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Optional

SCHEMA_VERSION = 1

#: Vocabulário de tipos de evento. Espelha, de forma agnóstica, os eventos do
#: ciclo de vida expostos pelo harness (Runs API / hooks) e o que é derivado do
#: banco de sessões.
EVENT_TYPES = frozenset(
    {
        "run_start",
        "run_end",
        "session_start",
        "session_end",
        "turn_start",
        "turn_end",
        "assistant_message",
        "assistant_delta",
        "llm_request",
        "llm_response",
        "tool_call_start",
        "tool_call_end",
        "tool_result",
        "steer",
        "interrupt",
        "approval_request",
        "compact",
        "budget_exhausted",
        "persistence_failure",
        "api_error",
        "error",
        "usage",
        "unknown",
    }
)

#: Origens suportadas pelo coletor.
EVENT_SOURCES = frozenset({"state_db", "logs", "runs_api", "spool", "fixture"})


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def digest(*parts: Any, length: int = 16) -> str:
    """Hash estável e curto usado para comparar argumentos/resultados sem guardar o conteúdo."""
    payload = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


@dataclass
class Event:
    """Um único evento observado, em formato normalizado."""

    type: str
    ts: str = field(default_factory=_utcnow_iso)
    source: str = "fixture"
    session_id: Optional[str] = None
    turn_id: Optional[str] = None
    task_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool: Optional[str] = None
    status: Optional[str] = None
    duration_ms: Optional[float] = None
    args_digest: Optional[str] = None
    result_digest: Optional[str] = None
    args_preview: Optional[str] = None
    text_preview: Optional[str] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    seq: Optional[int] = None
    meta: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            self.type = "unknown"
        if self.source not in EVENT_SOURCES:
            self.source = "fixture"

    def to_dict(self) -> Dict[str, Any]:
        data = dict(self.__dict__)
        # Campos vazios são omitidos para manter o NDJSON enxuto e legível.
        return {k: v for k, v in data.items() if v not in (None, {}, [], "")}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Event":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        payload = {k: v for k, v in data.items() if k in known}
        extra = {k: v for k, v in data.items() if k not in known}
        event = cls(**payload)
        if extra:
            event.meta = {**event.meta, **extra}
        return event

    # -- conveniência para os detectores ---------------------------------
    @property
    def tool_signature(self) -> Optional[str]:
        """Assinatura ``ferramenta+argumentos`` usada para detectar repetição."""
        if not self.tool:
            return None
        return f"{self.tool}:{self.args_digest or '-'}"

    @property
    def timestamp(self) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(self.ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError, TypeError):
            return None


def load_ndjson(path: str) -> Iterator[Event]:
    """Lê um arquivo NDJSON de eventos, ignorando linhas vazias/inválidas."""
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield Event.from_dict(json.loads(line))
            except json.JSONDecodeError:
                continue


def dump_ndjson(events: Iterator[Event], path: str) -> int:
    """Grava eventos em NDJSON (append-only, uma linha por evento). Retorna a contagem."""
    count = 0
    with open(path, "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(event.to_json())
            handle.write("\n")
            count += 1
    return count