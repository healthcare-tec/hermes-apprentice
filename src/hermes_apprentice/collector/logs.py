"""Leitor incremental (somente leitura) dos logs do Hermes.

O harness grava ``~/.hermes/logs/*.log`` (``errors.log``, ``gateway.log``, ...)
com segredos já redigidos pela própria aplicação. Ainda assim passamos tudo pela
nossa redação: o log é gerado por outro processo e não temos contrato de
conteúdo com ele.

O leitor é um *tailer*: guarda o offset já consumido por arquivo e nunca reescreve
nada. A classificação dos eventos é heurística e intencionalmente explícita — as
expressões reconhecidas estão em ``_CLASSIFIERS`` para que qualquer pessoa possa
auditar o que é detectado.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from ..redact import redact_text
from ..schema import Event

_TS_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
)
_LEVEL_RE = re.compile(r"\b(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\b")
_SESSION_RE = re.compile(r"(?P<sid>(?:sess[_-][A-Za-z0-9]+|\d{8}[_-]\d{6}[_-][A-Za-z0-9]{4,}))")

#: (tipo_de_evento, expressão regular). A ordem importa: o primeiro acerto vence.
_CLASSIFIERS: List[Tuple[str, re.Pattern[str]]] = [
    ("persistence_failure", re.compile(r"database is locked|session_persistence_failed|SQLITE_BUSY|vtable constructor failed")),
    ("budget_exhausted", re.compile(r"max_iterations_reached|iteration budget|turn_exit_reason\s*[=:]\s*max_iterations")),
    ("api_error", re.compile(r"api_request_error|rate.?limit|HTTP\s*429|HTTP\s*5\d\d|retry(?:ing)? attempt|backoff")),
    ("interrupt", re.compile(r"interrupted_by_user|interrupted_by_system|turn_liveness_watchdog|inactivity_watchdog|lease_lost")),
    ("compact", re.compile(r"\bcompress(?:ion|ing)?\b|\bcompact(?:ion)?\b")),
    ("tool_call_start", re.compile(r"\btool_call_start\b|\bcalling tool\b")),
    ("tool_call_end", re.compile(r"\btool_call_end\b|\btool (?:finished|completed)\b")),
    ("error", re.compile(r"\b(?:Traceback|Exception|ERROR|CRITICAL|FATAL)\b")),
]

ERROR_TYPE_HINTS = [
    ("persistence_locked", re.compile(r"database is locked|SQLITE_BUSY")),
    ("budget_exhausted", re.compile(r"max_iterations_reached")),
    ("provider_rate_limit", re.compile(r"rate.?limit|429")),
    ("provider_server_error", re.compile(r"5\d\d")),
]


def classify_line(line: str) -> Dict[str, Any]:
    """Classifica uma linha de log em um evento (ou ``unknown``)."""
    event_type = "unknown"
    for candidate, pattern in _CLASSIFIERS:
        if pattern.search(line):
            event_type = candidate
            break

    error_type = None
    for name, pattern in ERROR_TYPE_HINTS:
        if pattern.search(line):
            error_type = name
            break

    ts_match = _TS_RE.search(line)
    level_match = _LEVEL_RE.search(line)
    session_match = _SESSION_RE.search(line)
    return {
        "type": event_type,
        "ts": ts_match.group("ts").replace(" ", "T") if ts_match else None,
        "level": level_match.group("level") if level_match else None,
        "session_id": session_match.group("sid") if session_match else None,
        "error_type": error_type,
    }


def tail_file(
    path: Path,
    *,
    offset: int = 0,
    max_lines: int = 20000,
) -> Tuple[List[str], int]:
    """Lê linhas novas a partir de ``offset`` (sem alterar o arquivo). Devolve (linhas, novo_offset)."""
    try:
        size = path.stat().st_size
    except OSError:
        return [], offset
    if size < offset:  # rotação de log
        offset = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(offset)
        lines = []
        for _ in range(max_lines):
            line = handle.readline()
            if not line:
                break
            lines.append(line.rstrip("\n"))
        return lines, handle.tell()


def iter_events(
    logs_dir: str | Path,
    *,
    offsets: Optional[Dict[str, int]] = None,
    patterns: str = "*.log",
) -> Iterator[Event]:
    """Emite eventos a partir de todos os logs do diretório (mais recentes primeiro)."""
    directory = Path(logs_dir)
    if not directory.is_dir():
        return
    offsets = offsets if offsets is not None else {}
    files = sorted((p for p in directory.rglob(patterns) if p.is_file()),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        key = str(path)
        lines, new_offset = tail_file(path, offset=offsets.get(key, 0))
        offsets[key] = new_offset
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            info = classify_line(line)
            yield Event(
                type=info["type"],
                ts=info["ts"] or "",
                source="logs",
                session_id=info["session_id"],
                status="error" if info["level"] in {"ERROR", "CRITICAL", "FATAL"} else None,
                error_type=info["error_type"],
                error_message=redact_text(line, max_len=300) if info["type"] in {"error", "api_error", "persistence_failure", "budget_exhausted"} else None,
                text_preview=redact_text(line, max_len=200),
                meta={
                    "log_file": str(path.name),
                    "offset": offsets.get(key, 0) - len(lines) + index,
                    "level": info["level"],
                },
            )