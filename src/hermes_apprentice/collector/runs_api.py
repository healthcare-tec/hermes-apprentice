"""Consumo do stream estruturado de eventos do Hermes (Runs API, SSE).

O harness expõe ``GET /v1/runs/{run_id}/events`` como *Server-Sent Events* com o
ciclo de vida completo de uma execução (``run.started``, ``assistant.delta``,
``tool.started``, ``tool.completed``, ``run.completed``/``run.failed``/
``run.cancelled``). Este é o caminho **preferido** de observação: é um contrato
estável e publicado, em vez de parsing de saída de terminal.

Este módulo é um cliente HTTP puramente leitor: usa apenas ``GET``, nunca
``POST /v1/runs/{id}/steer`` nem ``/approval``. Nada do estado do agente é
alterado pela observação.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, Optional

from ..redact import redact_text
from ..schema import Event, digest

#: Mapa dos nomes de evento do harness para o nosso vocabulário normalizado.
SSE_EVENT_MAP: Dict[str, str] = {
    "run.started": "run_start",
    "run.started ": "run_start",
    "run.steered": "steer",
    "message.delta": "assistant_delta",
    "assistant.delta": "assistant_delta",
    "assistant.completed": "assistant_message",
    "tool.start": "tool_call_start",
    "tool.started": "tool_call_start",
    "tool.generating": "tool_call_start",
    "tool.complete": "tool_call_end",
    "tool.completed": "tool_call_end",
    "agent.error": "error",
    "run.completed": "run_end",
    "run.failed": "run_end",
    "run.cancelled": "run_end",
    "run.interrupted": "run_end",
    "usage": "usage",
    "session_id": "session_start",
}


def parse_sse_lines(lines: Iterator[str], *, run_id: Optional[str] = None) -> Iterator[Event]:
    """Converte linhas SSE em eventos normalizados.

    Suporta tanto ``data:`` puro quanto ``event:`` + ``data:``. Linhas de
    comentário (``:``) e keep-alives são ignoradas.
    """
    event_name: Optional[str] = None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip()
            continue
        if not line.startswith("data:"):
            continue
        payload_text = line.split(":", 1)[1].strip()
        if payload_text in {"[DONE]", ""}:
            event_name = None
            continue
        try:
            payload: Dict[str, Any] = json.loads(payload_text)
        except json.JSONDecodeError:
            continue
        name = payload.get("type") or payload.get("event") or event_name or "unknown"
        yield normalize_run_event(name, payload, run_id=run_id)
        event_name = None


def normalize_run_event(name: str, payload: Dict[str, Any], *, run_id: Optional[str] = None) -> Event:
    """Normaliza um evento da Runs API para o esquema interno."""
    event_type = SSE_EVENT_MAP.get(name, "unknown")
    status = payload.get("status")
    error = payload.get("error")
    tool_args = payload.get("args") or payload.get("tool_input") or payload.get("input")
    result = payload.get("result") or payload.get("output") or payload.get("content")

    kwargs: Dict[str, Any] = dict(
        type=event_type,
        ts=payload.get("ts") or payload.get("timestamp") or "",
        source="runs_api",
        session_id=payload.get("session_id"),
        turn_id=str(payload["turn_id"]) if payload.get("turn_id") is not None else None,
        task_id=payload.get("task_id"),
        tool_call_id=payload.get("call_id") or payload.get("tool_call_id"),
        tool=payload.get("tool") or payload.get("tool_name"),
        status=status,
        duration_ms=payload.get("duration_ms"),
        model=payload.get("model"),
        provider=payload.get("provider"),
        meta={"run_id": run_id or payload.get("run_id"), "sse_event": name},
    )

    if event_type == "run_end":
        kwargs["status"] = {
            "run.completed": "completed",
            "run.failed": "failed",
            "run.cancelled": "cancelled",
            "run.interrupted": "interrupted",
        }.get(name, status)
        if error:
            kwargs["error_message"] = redact_text(str(error), max_len=300)
            kwargs["error_type"] = payload.get("error_type") or name
        meta = dict(kwargs["meta"])
        for flag in ("completed", "partial", "interrupted", "turn_exit_reason"):
            if flag in payload:
                meta[flag] = payload[flag]
        kwargs["meta"] = meta
    elif event_type == "tool_call_start":
        args_obj = tool_args if isinstance(tool_args, (dict, list, str)) else {}
        kwargs["args_digest"] = digest(kwargs.get("tool"), args_obj)
        kwargs["args_preview"] = redact_text(json.dumps(args_obj, ensure_ascii=False), max_len=240)
    elif event_type in {"tool_call_end", "tool_result"}:
        kwargs["result_digest"] = digest(result) if result is not None else None
        kwargs["text_preview"] = redact_text(str(result or payload.get("summary") or ""), max_len=300)
        if payload.get("is_error") or status in {"error", "failed"}:
            kwargs["status"] = "error"
    elif event_type == "assistant_message":
        kwargs["text_preview"] = redact_text(str(payload.get("text") or payload.get("final_answer") or ""), max_len=400)
    elif event_type == "assistant_delta":
        kwargs["text_preview"] = redact_text(str(payload.get("delta") or payload.get("text") or ""), max_len=120)
    elif event_type == "usage":
        kwargs["meta"] = {**kwargs["meta"], **(payload.get("usage") or {})}

    return Event(**kwargs)


def iter_events(
    base_url: str,
    run_id: str,
    *,
    api_key: Optional[str] = None,
    timeout: float = 300.0,
    poll_interval: float = 0.0,
) -> Iterator[Event]:
    """Abre o SSE de uma execução e itera os eventos até o fim do stream."""
    url = f"{base_url.rstrip('/')}/v1/runs/{run_id}/events"
    request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")

    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - base_url do usuário
        def _lines() -> Iterator[str]:
            for raw in response:
                if time.monotonic() - started > timeout:
                    return
                yield raw.decode("utf-8", errors="replace")
                if poll_interval:
                    time.sleep(poll_interval)

        yield from parse_sse_lines(_lines(), run_id=run_id)


def probe_run_status(base_url: str, run_id: str, *, api_key: Optional[str] = None, timeout: float = 15.0) -> Dict[str, Any]:
    """Consulta ``GET /v1/runs/{id}`` (somente leitura) para saber o estado atual."""
    url = f"{base_url.rstrip('/')}/v1/runs/{run_id}"
    request = urllib.request.Request(url)
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {"error": str(exc)}