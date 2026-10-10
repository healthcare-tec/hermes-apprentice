"""Prompts do classificador.

Decisões deliberadas para um modelo pequeno (0.6B) rodando em CPU:

* **Inglês.** Modelos pequenos seguem instruções em inglês com mais fidelidade do
  que em português; a tradução fica na camada de relatório.
* **Rótulo fechado.** Três rótulos apenas (``normal``/``loop``/``failure``) e um
  subtipo opcional. Vocabulário aberto quebra o JSON em modelos pequenos.
* **Sem "pense passo a passo".** O modo de raciocínio é desligado
  (``enable_thinking=False``): em CPU o custo por token é proibitivo e a
  justificativa curta já vem no campo ``reason``.
* **Saída JSON estrita.** O parser em ``qwen.py`` tolera cercas de código e texto
  extra, mas o alvo é uma única linha JSON.
"""

from __future__ import annotations

from typing import Sequence

from ..schema import Event

LABELS = ("normal", "loop", "failure")

LOOP_TYPES = (
    "repetition",
    "no_progress",
    "cycle",
    "oscillation",
    "retry_storm",
    "budget_exhausted",
    "persistence_locked",
    "none",
)

SYSTEM_PROMPT = """You are a log analyst for an AI agent harness called Hermes.
You receive a numbered window of normalized agent events (tool calls, tool results, errors).
Decide whether the window shows a healthy run, a stuck loop, or a hard failure.

Labels:
- "normal": progress is being made; actions and results vary.
- "loop": the agent repeats work without changing the world (same tool+arguments,
  same result again and again, alternating tools with no convergence).
- "failure": the run ends or is aborted by an external cause (provider errors,
  retries storm, iteration budget exhausted, database lock).

Answer with ONE JSON object on a single line, no markdown, no extra text:
{"label": "normal|loop|failure", "loop_type": "repetition|no_progress|cycle|oscillation|retry_storm|budget_exhausted|persistence_locked|none", "confidence": 0.0, "reason": "<= 15 words"}
"""


def render_event(index: int, event: Event) -> str:
    """Renderiza um evento em uma linha compacta, sem conteúdo sensível."""
    parts = [f"{index:02d}", event.type]
    if event.tool:
        parts.append(f"tool={event.tool}")
    if event.args_digest:
        parts.append(f"args={event.args_digest[:8]}")
    if event.result_digest:
        parts.append(f"result={event.result_digest[:8]}")
    if event.status:
        parts.append(f"status={event.status}")
    if event.duration_ms is not None:
        parts.append(f"ms={int(event.duration_ms)}")
    if event.error_type:
        parts.append(f"err={event.error_type}")
    if event.meta.get("turn_exit_reason"):
        parts.append(f"exit={event.meta['turn_exit_reason']}")
    if event.text_preview:
        preview = event.text_preview.replace("\n", " ")[:80]
        parts.append(f'text="{preview}"')
    return " ".join(parts)


def render_window(events: Sequence[Event], *, start: int = 0, max_events: int = 16) -> str:
    """Janela de eventos como texto compacto — entrada do modelo."""
    lines = [render_event(start + offset, event) for offset, event in enumerate(events[:max_events])]
    return "\n".join(lines)


def build_messages(events: Sequence[Event], *, start: int = 0, max_events: int = 16) -> list[dict]:
    """Mensagens no formato de chat do Qwen3."""
    window = render_window(events, start=start, max_events=max_events)
    user = f"Event window:\n{window}\n\nClassify this window."
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]