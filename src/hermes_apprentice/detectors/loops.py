"""Detectores de loop.

Quatro assinaturas distintas, porque "loop" no harness não é um fenômeno único:

* ``repetition``  — a mesma chamada (ferramenta + argumentos **idênticos**) se repete.
* ``no_progress`` — as chamadas variam, mas o resultado é sempre o mesmo: o agente
  continua agindo sem que o mundo mude. É a assinatura do *stub* de deduplicação
  de ``read_file`` (relatado no harness como caso de loop infinito).
* ``cycle``       — alternância de ferramentas com período fixo (A→B→A→B),
  típica de busca que nunca converge.
* ``oscillation`` — ida e volta entre exatamente dois estados de resultado
  (resultado X, resultado Y, resultado X, ...), indicando correção desfeita.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Dict, List, Optional, Sequence

from ..schema import Event
from .base import DetectorConfig, Finding, group_by_session, seconds_between


def detect_repetition(events: Sequence[Event], config: DetectorConfig) -> List[Finding]:
    """Chamadas repetidas com a mesma assinatura ferramenta+argumentos."""
    findings: List[Finding] = []
    for session_id, indices in group_by_session(events).items():
        buckets: Dict[str, List[int]] = defaultdict(list)
        for index in indices:
            event = events[index]
            if event.type == "tool_call_start" and event.tool_signature:
                buckets[event.tool_signature].append(index)
        for signature, bucket in buckets.items():
            if len(bucket) < config.repetition_min:
                continue
            window = [events[i] for i in bucket]
            span = seconds_between(window[0].ts, window[-1].ts)
            if config.burst_window_s and span is not None and span > config.burst_window_s * max(1, len(bucket) - 1):
                continue
            tool = window[0].tool
            findings.append(
                Finding(
                    rule="loop.repetition",
                    category="loop",
                    severity="high" if len(bucket) >= config.repetition_min * 2 else "medium",
                    message=(
                        f"A ferramenta `{tool}` foi chamada {len(bucket)} vezes com argumentos idênticos "
                        f"na sessão {session_id} sem mudança de estado."
                    ),
                    session_id=session_id,
                    start_ts=window[0].ts,
                    end_ts=window[-1].ts,
                    evidence=bucket,
                    detail={
                        "tool": tool,
                        "signature": signature,
                        "occurrences": len(bucket),
                        "span_seconds": span,
                        "threshold": config.repetition_min,
                        "args_preview": window[0].args_preview,
                    },
                )
            )
    return findings


def detect_no_progress(events: Sequence[Event], config: DetectorConfig) -> List[Finding]:
    """Mesmo resultado repetido para a mesma ferramenta — ação sem progresso."""
    findings: List[Finding] = []
    for session_id, indices in group_by_session(events).items():
        buckets: Dict[str, List[int]] = defaultdict(list)
        for index in indices:
            event = events[index]
            if event.type in {"tool_call_end", "tool_result"} and event.tool and event.result_digest:
                buckets[f"{event.tool}:{event.result_digest}"].append(index)
        for key, bucket in buckets.items():
            if len(bucket) < config.no_progress_min:
                continue
            tool = key.split(":", 1)[0]
            window = [events[i] for i in bucket]
            is_stub = len(bucket) >= config.dedup_stub_min and any(
                marker in str(events[i].text_preview or "").lower()
                for i in bucket
                for marker in ("already read", "dedup", "unchanged", "já lido", "sem alteração", "identical to previous")
            )
            findings.append(
                Finding(
                    rule="loop.no_progress",
                    category="loop",
                    severity="high" if is_stub else "medium",
                    message=(
                        f"A ferramenta `{tool}` devolveu o mesmo resultado {len(bucket)} vezes "
                        f"({'(sinal de stub de deduplicação)' if is_stub else 'sem mudança de estado'})."
                    ),
                    session_id=session_id,
                    start_ts=window[0].ts,
                    end_ts=window[-1].ts,
                    evidence=bucket,
                    detail={
                        "tool": tool,
                        "occurrences": len(bucket),
                        "dedup_stub_suspected": is_stub,
                        "threshold": config.no_progress_min,
                        "result_preview": window[0].text_preview,
                    },
                )
            )
    return findings


def detect_cycle(events: Sequence[Event], config: DetectorConfig) -> List[Finding]:
    """Alternância periódica de ferramentas (A→B→A→B...)."""
    findings: List[Finding] = []
    for session_id, indices in group_by_session(events).items():
        sequence: List[tuple[int, str]] = [
            (i, events[i].tool or "unknown")
            for i in indices
            if events[i].type == "tool_call_start"
        ]
        names = [name for _, name in sequence]
        if len(names) < config.cycle_min_repeats * 2:
            continue

        # O ciclo pode começar em qualquer ponto da sessão (o agente pode ter
        # feito trabalho útil antes de entrar no laço). Por isso varremos todas
        # as posições de início, do maior para o menor período, e ficamos com o
        # trecho mais longo que não se sobrepõe a outro já reportado.
        candidates: List[tuple[int, int, int]] = []  # (length, start, period)
        for start in range(len(names)):
            for period in range(2, config.cycle_max_period + 1):
                if start + period * config.cycle_min_repeats > len(names):
                    break
                pattern = names[start : start + period]
                if len(set(pattern)) < 2:
                    continue  # ferramenta única já é coberto por `repetition`
                repeats = 0
                while (
                    start + (repeats + 1) * period <= len(names)
                    and names[start + repeats * period : start + (repeats + 1) * period] == pattern
                ):
                    repeats += 1
                if repeats < config.cycle_min_repeats:
                    continue
                covered = sequence[start : start + period * repeats]
                # Só acusa ciclo se os argumentos variarem — senão é repetição pura.
                digests = {events[i].args_digest for i, _ in covered}
                if len(digests) <= 1:
                    continue
                candidates.append((period * repeats, start, period))

        if not candidates:
            continue
        candidates.sort(key=lambda item: -item[0])
        reported: List[Finding] = []
        occupied: set[int] = set()
        for length, start, period in candidates:
            covered = sequence[start : start + length]
            indexes = [i for i, _ in covered]
            if occupied & set(indexes):
                continue
            occupied.update(indexes)
            repeats = length // period
            pattern = names[start : start + period]
            digests = {events[i].args_digest for i in indexes}
            reported.append(
                Finding(
                    rule="loop.cycle",
                    category="loop",
                    severity="high" if repeats >= config.cycle_min_repeats + 1 else "medium",
                    message=(
                        f"Ciclo de ferramentas {'→'.join(pattern)} repetido {repeats} vezes "
                        f"na sessão {session_id}, com argumentos sempre diferentes."
                    ),
                    session_id=session_id,
                    start_ts=events[indexes[0]].ts,
                    end_ts=events[indexes[-1]].ts,
                    evidence=indexes,
                    detail={
                        "pattern": pattern,
                        "period": period,
                        "repeats": repeats,
                        "distinct_argument_digests": len(digests),
                        "threshold": config.cycle_min_repeats,
                    },
                )
            )
        findings.extend(reported)
    return findings


def detect_oscillation(events: Sequence[Event], config: DetectorConfig) -> List[Finding]:
    """Resultados alternando entre dois estados (correção desfeita)."""
    findings: List[Finding] = []
    for session_id, indices in group_by_session(events).items():
        results: List[tuple[int, str, str]] = [
            (i, events[i].tool or "unknown", events[i].result_digest or "")
            for i in indices
            if events[i].type in {"tool_call_end", "tool_result"} and events[i].result_digest
        ]
        names = [f"{tool}:{digest}" for _, tool, digest in results]
        if len(names) < 6:
            continue
        # Oscilação exige a **mesma** ferramenta alternando resultados: dois
        # estados de ferramentas diferentes não configuram correção desfeita.
        tool_names = {tool for _, tool, _ in results}
        if len(tool_names) != 1:
            continue
        # Janela deslizante: procura alternância estrita entre dois valores.
        best: Optional[tuple[int, int, List[int]]] = None
        start = 0
        while start < len(names) - 3:
            pair = {names[start], names[start + 1]}
            if len(pair) != 2:
                start += 1
                continue
            end = start + 2
            while end < len(names) and names[end] in pair:
                end += 1
            length = end - start
            if length >= 6 and (best is None or length > best[0]):
                best = (length, start, [i for i, _, _ in results[start:end]])
            start = end if end > start else start + 1
        if best is None:
            continue
        length, start, evidence = best
        window = [events[i] for i in evidence]
        findings.append(
            Finding(
                rule="loop.oscillation",
                category="loop",
                severity="high" if length >= 8 else "medium",
                message=(
                    f"Resultados alternando entre dois estados por {length} chamadas "
                    f"na sessão {session_id}: a correção aplicada não se mantém."
                ),
                session_id=session_id,
                start_ts=window[0].ts,
                end_ts=window[-1].ts,
                evidence=evidence,
                detail={
                    "alternations": length,
                    "states": sorted({names[start], names[start + 1]}),
                    "tool": results[start][1],
                },
            )
        )
    return findings
