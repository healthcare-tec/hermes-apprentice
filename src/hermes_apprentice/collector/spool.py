"""Leitura do *spool* NDJSON alimentado por um hook de observação opcional.

Modo padrão do projeto: **leitura passiva** (``state.db``, logs, Runs API). Nada
toca a configuração do Hermes.

Este módulo cobre o modo opcional: o usuário registra um hook de shell
(``~/.hermes/config.yaml`` → bloco ``hooks:``) que apenas *encaminha* o payload
JSON do evento para um arquivo NDJSON em um diretório de spool. O hook:

* não devolve diretivas (não bloqueia, não modifica argumentos, não injeta
  contexto) — é um observador puro;
* escreve apenas no diretório do spool, nunca no Hermes;
* é registrado pelo usuário, com consentimento explícito por par ``(evento, comando)``.

Este leitor nunca carrega o hook: apenas lê o NDJSON resultante.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from ..redact import redact_text
from ..schema import Event, digest

#: Marca que identifica registros produzidos pelo hook de observação.
OBSERVER_MARKER = "hermes-apprentice"

#: Eventos de hook do Hermes → vocabulário normalizado.
HOOK_EVENT_MAP: Dict[str, str] = {
    "pre_tool_call": "tool_call_start",
    "post_tool_call": "tool_call_end",
    "transform_tool_result": "tool_result",
    "pre_llm_call": "llm_request",
    "post_llm_call": "llm_response",
    "pre_api_request": "llm_request",
    "post_api_request": "llm_response",
    "api_request_error": "api_error",
    "transform_llm_output": "assistant_message",
    "on_session_finalize": "session_end",
    "subagent_stop": "session_end",
}


def normalize_hook_record(record: Dict[str, Any]) -> Optional[Event]:
    """Converte um registro do hook em evento normalizado (``None`` se não for nosso)."""
    marker = record.get("observer") or record.get("tool") or ""
    if OBSERVER_MARKER not in str(marker):
        return None

    hook_name = str(record.get("hook_event_name") or record.get("event") or "unknown")
    event_type = HOOK_EVENT_MAP.get(hook_name, "unknown")
    tool_input = record.get("tool_input") or record.get("args") or {}
    extra = record.get("extra") or {}

    kwargs: Dict[str, Any] = dict(
        type=event_type,
        ts=str(record.get("ts") or record.get("timestamp") or ""),
        source="spool",
        session_id=record.get("session_id"),
        turn_id=str(extra.get("turn_id")) if extra.get("turn_id") is not None else None,
        task_id=extra.get("task_id"),
        tool_call_id=extra.get("tool_call_id") or record.get("tool_call_id"),
        tool=record.get("tool_name") or record.get("tool"),
        status=record.get("status"),
        duration_ms=record.get("duration_ms"),
        error_type=record.get("error_type"),
        model=record.get("model"),
        provider=record.get("provider"),
        meta={"hook_event": hook_name, "cwd": record.get("cwd"), "profile": record.get("profile")},
    )

    if event_type == "tool_call_start":
        kwargs["args_digest"] = digest(kwargs.get("tool"), tool_input)
        kwargs["args_preview"] = redact_text(json.dumps(tool_input, ensure_ascii=False), max_len=240)
    elif event_type in {"tool_call_end", "tool_result"}:
        result = record.get("result")
        kwargs["result_digest"] = digest(result) if result is not None else None
        kwargs["text_preview"] = redact_text(str(result or record.get("error_message") or ""), max_len=300)
        if record.get("error_type"):
            kwargs["status"] = "error"
    elif event_type in {"llm_response", "assistant_message"}:
        kwargs["text_preview"] = redact_text(
            str(record.get("assistant_response") or record.get("response_text") or ""), max_len=300
        )
        usage = record.get("usage")
        if isinstance(usage, dict):
            kwargs["meta"] = {**kwargs["meta"], **usage}
    elif event_type == "api_error":
        kwargs["error_message"] = redact_text(str(record.get("error") or ""), max_len=300)
        kwargs["status"] = "error"
    return Event(**kwargs)


def iter_events(spool_dir: str | Path, *, pattern: str = "*.ndjson") -> Iterator[Event]:
    """Lê todos os registros do diretório de spool, ignorando os que não são nossos."""
    directory = Path(spool_dir)
    if not directory.is_dir():
        return
    for path in sorted(directory.glob(pattern)):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    event = normalize_hook_record(record)
                    if event is not None:
                        yield event
        except OSError:
            continue