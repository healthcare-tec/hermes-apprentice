"""Detectores de falha (não-loop): orçamento, persistência e tempestade de retries."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Dict, List, Sequence

from ..schema import Event
from .base import DetectorConfig, Finding, group_by_session, seconds_between


def detect_budget_exhausted(events: Sequence[Event], config: DetectorConfig) -> List[Finding]:
    """Orçamento de iterações/turnos esgotado — a execução termina sem resposta final."""
    findings: List[Finding] = []
    for session_id, indices in group_by_session(events).items():
        hits = [
            i
            for i in indices
            if events[i].type == "budget_exhausted"
            or "max_iterations" in str(events[i].meta.get("turn_exit_reason") or "")
            or "max_iterations" in str(events[i].error_message or "")
        ]
        if not hits:
            continue
        window = [events[i] for i in hits]
        reason = next(
            (
                events[i].meta.get("turn_exit_reason")
                for i in hits
                if events[i].meta.get("turn_exit_reason")
            ),
            "orçamento de iterações esgotado",
        )
        findings.append(
            Finding(
                rule="failure.budget_exhausted",
                category="failure",
                severity="high",
                message=f"Turno encerrado por esgotamento de orçamento ({reason}) na sessão {session_id}.",
                session_id=session_id,
                start_ts=window[0].ts,
                end_ts=window[-1].ts,
                evidence=hits,
                detail={"reason": reason, "occurrences": len(hits)},
            )
        )
    return findings


def detect_persistence_locked(events: Sequence[Event], config: DetectorConfig) -> List[Finding]:
    """Falha de persistência por contenção de escrita no ``state.db``."""
    findings: List[Finding] = []
    for session_id, indices in group_by_session(events).items():
        hits = [
            i
            for i in indices
            if events[i].type == "persistence_failure"
            or "session_persistence_failed" in str(events[i].error_message or "")
        ]
        if not hits:
            continue
        window = [events[i] for i in hits]
        findings.append(
            Finding(
                rule="failure.persistence_locked",
                category="failure",
                severity="high",
                message=(
                    f"{len(hits)} falha(s) de persistência na sessão {session_id}: o turno foi abortado "
                    f"por lock de escrita no state.db."
                ),
                session_id=session_id,
                start_ts=window[0].ts,
                end_ts=window[-1].ts,
                evidence=hits,
                detail={"occurrences": len(hits), "sample": window[0].error_message},
            )
        )
    return findings


def detect_retry_storm(events: Sequence[Event], config: DetectorConfig) -> List[Finding]:
    """Rajada de erros de provedor — o agente gasta o orçamento tentando de novo."""
    findings: List[Finding] = []
    for session_id, indices in group_by_session(events).items():
        errors = [
            i
            for i in indices
            if events[i].type in {"api_error", "error"}
            and (
                events[i].error_type
                in {"provider_rate_limit", "provider_server_error", "api_request_error", "retryable"}
                or "retry" in str(events[i].error_message or "").lower()
                or "429" in str(events[i].error_message or "")
            )
        ]
        if len(errors) < config.retry_storm_min:
            continue
        window = [events[i] for i in errors]
        span = seconds_between(window[0].ts, window[-1].ts)
        if config.burst_window_s and span is not None and span > config.burst_window_s:
            continue
        kinds = Counter(events[i].error_type for i in errors if events[i].error_type)
        findings.append(
            Finding(
                rule="failure.retry_storm",
                category="failure",
                severity="high" if len(errors) >= config.retry_storm_min * 2 else "medium",
                message=(
                    f"{len(errors)} erros de provedor em {span if span is not None else '?'}s "
                    f"na sessão {session_id} (categorias: {dict(kinds)})."
                ),
                session_id=session_id,
                start_ts=window[0].ts,
                end_ts=window[-1].ts,
                evidence=errors,
                detail={"occurrences": len(errors), "span_seconds": span, "categories": dict(kinds)},
            )
        )
    return findings


def detect_run_failures(events: Sequence[Event], config: DetectorConfig) -> List[Finding]:
    """Execuções que terminam sem resposta final (``run_end`` não concluído)."""
    findings: List[Finding] = []
    for session_id, indices in group_by_session(events).items():
        terminal = [
            i
            for i in indices
            if events[i].type == "run_end" and events[i].status not in {"completed", "ok", None}
        ]
        if not terminal:
            continue
        window = [events[i] for i in terminal]
        statuses = Counter(events[i].status for i in terminal)
        findings.append(
            Finding(
                rule="failure.run_not_completed",
                category="failure",
                severity="medium",
                message=f"Execução terminou em {dict(statuses)} na sessão {session_id}, sem resposta final.",
                session_id=session_id,
                start_ts=window[0].ts,
                end_ts=window[-1].ts,
                evidence=terminal,
                detail={"statuses": dict(statuses)},
            )
        )
    return findings