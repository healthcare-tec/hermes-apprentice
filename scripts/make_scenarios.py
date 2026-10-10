#!/usr/bin/env python3
"""Gera os cinco cenários reproduzíveis de falha e correção.

Cada cenário é um par ``events.ndjson`` (fluxo normalizado de eventos) +
``scenario.json`` (metadados, zonas de rótulo e correção aplicada). Os eventos
são **sintéticos porém fiéis à assinatura** de falhas documentadas do harness
Hermes — a referência de cada modo de falha está no campo ``reference`` do
cenário.

Uso:
    python scripts/make_scenarios.py --out scenarios
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

BASE_TIME = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)
STEP_SECONDS = 3
WINDOW_SIZE = 12


def digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


class Trace:
    """Acumulador de eventos com carimbo de tempo e numeração sequencial."""

    def __init__(self, session_id: str, source: str = "fixture") -> None:
        self.session_id = session_id
        self.source = source
        self.events: List[Dict[str, Any]] = []
        self.clock = BASE_TIME

    def add(self, type_: str, **fields: Any) -> None:
        self.clock += timedelta(seconds=STEP_SECONDS)
        event: Dict[str, Any] = {
            "schema_version": 1,
            "type": type_,
            "ts": self.clock.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "source": self.source,
            "session_id": self.session_id,
            "seq": len(self.events) + 1,
        }
        event.update({k: v for k, v in fields.items() if v is not None})
        self.events.append(event)

    def tool(self, name: str, args: Dict[str, Any], *, status: str = "ok",
             result: str | None = None, duration_ms: int = 120, call_suffix: str = "") -> None:
        """Emite o par chamada/resultado de uma ferramenta."""
        call_id = f"call_{len(self.events):03d}{call_suffix}"
        self.add(
            "tool_call_start",
            tool=name,
            tool_call_id=call_id,
            args_digest=digest(name, args),
            args_preview=json.dumps(args, ensure_ascii=False)[:200],
        )
        self.add(
            "tool_call_end",
            tool=name,
            tool_call_id=call_id,
            status=status,
            duration_ms=duration_ms,
            result_digest=digest(result) if result is not None else None,
            text_preview=(result or "")[:240],
        )

    def turn(self, user_text: str, assistant_text: str = "") -> None:
        self.add("turn_start", text_preview=user_text[:200])
        if assistant_text:
            self.add("assistant_message", text_preview=assistant_text[:200])


def _phase(events: Sequence[Dict[str, Any]], label: str, loop_type: str, phase: str) -> Dict[str, Any]:
    return {
        "phase": phase,
        "label": label,
        "loop_type": loop_type,
        "from_event": 0,  # preenchido em write_scenario
        "to_event": 0,
        "_length": len(events),
    }


#: Ferramentas usadas para preencher zonas saudáveis. São **nomes distintos**
#: de propósito: um ciclo de período curto (A→B→A→B) precisa de 6 chamadas numa
#: janela de 12 eventos para ser acusado, e um rodízio de 8 ferramentas nunca
#: repete um padrão duas vezes dentro de uma janela.
_SAFE_TOOLS = [
    "read_file",
    "terminal",
    "session_search",
    "edit_file",
    "web_search",
    "web_extract",
    "memory_write",
    "skill_list",
]


def pad_normal(
    trace: "Trace",
    events: List[Dict[str, Any]],
    *,
    target: int = 18,
) -> List[Dict[str, Any]]:
    """Preenche uma fase saudável até ``target`` eventos com passos sem repetição.

    Cada passo usa ferramenta, argumentos e resultado únicos, para que a fase
    saudável permaneça saudável segundo os próprios detectores — se o
    preenchimento criasse um loop, os rótulos do cenário seriam mentira.
    """
    counter = 0
    while len(events) < target:
        counter += 1
        before = len(trace.events)
        tool = _SAFE_TOOLS[(counter - 1) % len(_SAFE_TOOLS)]
        trace.tool(
            tool,
            {"slot": f"revisao-{counter}", "target": tool},
            result=f"resultado distinto do passo de revisão {counter}",
            duration_ms=110 + counter * 7,
        )
        events.extend(trace.events[before:])
    return events
def write_scenario(
    out_root: Path,
    *,
    scenario_id: str,
    title: str,
    failure_mode: str,
    description: str,
    reference: str,
    expected_rules: Sequence[str],
    expected_verdict: str,
    correction: Dict[str, str],
    phases: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Grava um cenário no disco e devolve os metadados (sem os eventos)."""
    directory = out_root / scenario_id
    directory.mkdir(parents=True, exist_ok=True)

    all_events: List[Dict[str, Any]] = []
    zones: List[Dict[str, Any]] = []
    cursor = 0
    for phase in phases:
        events: List[Dict[str, Any]] = phase["events"]
        start = cursor
        end = cursor + len(events) - 1
        all_events.extend(events)
        cursor = end + 1
        # Só criamos zona se houver material para pelo menos uma janela cheia.
        if len(events) >= WINDOW_SIZE:
            zones.append(
                {
                    "phase": phase["phase"],
                    "label": phase["label"],
                    "loop_type": phase["loop_type"],
                    "from_event": start,
                    "to_event": end,
                }
            )

    events_file = "events.ndjson"
    with (directory / events_file).open("w", encoding="utf-8") as handle:
        for _, event in enumerate(all_events):
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            handle.write("\n")

    meta = {
        "schema_version": 1,
        "id": scenario_id,
        "title": title,
        "kind": "failure-scenario",
        "hermes_failure_mode": failure_mode,
        "description": description,
        "reference": reference,
        "expected_rules": list(expected_rules),
        "expected_verdict": expected_verdict,
        "correction": correction,
        "events_file": events_file,
        "event_count": len(all_events),
        "window_size": WINDOW_SIZE,
        "label_zones": zones,
        "zone_counts": {zone["label"]: sum(1 for z in zones if z["label"] == zone["label"]) for zone in zones},
    }
    (directory / "scenario.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return meta


# ---------------------------------------------------------------------------
# Cenário 1 — loop de read_file com stub de deduplicação
# ---------------------------------------------------------------------------
def scenario_read_file_dedup(root: Path) -> Dict[str, Any]:
    session = "sess_s1_read_file_dedup"
    trace = Trace(session)

    normal_start = len(trace.events)
    trace.turn("Revise o texto do manual de integração e ajuste as instruções ambíguas.", "Vou ler o manual e propor ajustes.")
    trace.add("llm_request", model="qwen3-0.6b", provider="local")
    trace.tool("read_file", {"path": "docs/manual.md", "limit": 200}, result="conteúdo do manual…")
    trace.tool("read_file", {"path": "docs/glossario.md", "limit": 120}, result="lista de termos…")
    trace.tool("edit_file", {"path": "docs/manual.md", "patch": "@@ seção 3"}, result="patch aplicado")
    trace.tool("terminal", {"command": "git diff --stat"}, result="1 file changed")
    trace.add("llm_response", text_preview="Ajustei a seção 3 do manual.", meta={"usage": {"output_tokens": 210}})
    trace.add("assistant_message", text_preview="Ajustei a seção 3 e removi a ambiguidade do passo 4.")
    trace.tool("read_file", {"path": "docs/manual.md", "limit": 200}, result="conteúdo do manual…")
    trace.add("tool_result", tool="edit_file", result_digest=digest("patch aplicado"), text_preview="patch aplicado", status="ok")
    normal = pad_normal(trace, trace.events[normal_start:])

    loop_start = len(trace.events)
    stub = "Arquivo já lido nesta sessão; conteúdo idêntico ao anterior retornado por deduplicação."
    for _ in range(12):
        trace.tool("read_file", {"path": "docs/manual.md", "limit": 200}, result=stub, duration_ms=8)
    trace.add("assistant_message", text_preview="Vou reler o arquivo para confirmar o trecho.")
    loop = trace.events[loop_start:]

    fix_start = len(trace.events)
    trace.add("steer", text_preview="Pare de reler o mesmo arquivo: use o conteúdo que você já tem ou leia outro trecho com offset.", meta={"queued": True})
    trace.tool("read_file", {"path": "docs/manual.md", "offset": 400, "limit": 80}, result="trecho seguinte do manual…")
    trace.tool("edit_file", {"path": "docs/manual.md", "patch": "@@ passo 4 reescrito"}, result="patch aplicado no passo 4")
    trace.tool("terminal", {"command": "git diff --stat"}, result="2 files changed")
    trace.add("llm_response", text_preview="Corrigi o passo 4 com base no trecho lido.", meta={"usage": {"output_tokens": 180}})
    trace.add("assistant_message", text_preview="Pronto: passo 4 reescrito e verificado.")
    trace.add("turn_end", status="ok")
    trace.add("run_end", status="completed", meta={"completed": True})
    trace.tool("read_file", {"path": "docs/glossario.md", "offset": 10, "limit": 40}, result="termos adicionais…")
    trace.add("session_end", status="ok", meta={"end_reason": "user_exit"})
    correction_events = trace.events[fix_start:]

    return write_scenario(
        root,
        scenario_id="s1_loop_read_file_dedup",
        title="Loop de read_file com stub de deduplicação",
        failure_mode="Releitura infinita do mesmo arquivo: o harness devolve um stub de deduplicação e o modelo interpreta como resultado novo.",
        description=(
            "Após uma edição bem-sucedida, o agente volta a chamar `read_file` com os mesmos argumentos. "
            "O resultado é sempre o mesmo *stub* de deduplicação. Sem detectar a repetição, o agente gasta "
            "o orçamento de turno relendo o mesmo conteúdo."
        ),
        reference="https://github.com/NousResearch/hermes-agent/issues/15759",
        expected_rules=["loop.repetition", "loop.no_progress"],
        expected_verdict="loop",
        correction={
            "strategy": "steer",
            "description": "Injeção de orientação (`/steer`) proibindo a releitura e sugerindo leitura paginada com `offset`.",
        },
        phases=[
            {"phase": "saudável", "label": "normal", "loop_type": "none", "events": normal},
            {"phase": "loop", "label": "loop", "loop_type": "repetition", "events": loop},
            {"phase": "correção", "label": "normal", "loop_type": "none", "events": correction_events},
        ],
    )


# ---------------------------------------------------------------------------
# Cenário 2 — orçamento de iterações esgotado com resultados idênticos
# ---------------------------------------------------------------------------
def scenario_budget_exhausted(root: Path) -> Dict[str, Any]:
    session = "sess_s2_budget_exhausted"
    trace = Trace(session)

    start = len(trace.events)
    trace.turn("Faça o teste de integração passar.", "Vou rodar a suíte e corrigir o que falhar.")
    trace.tool("terminal", {"command": "pytest tests/test_api.py -q"}, status="error", result="3 failed, 12 passed", duration_ms=5400)
    trace.tool("read_file", {"path": "tests/test_api.py", "limit": 120}, result="def test_cria_pedido(): ...")
    trace.tool("edit_file", {"path": "src/api.py", "patch": "@@ valida payload"}, result="patch aplicado")
    trace.tool("terminal", {"command": "pytest tests/test_api.py -q"}, status="error", result="2 failed, 13 passed", duration_ms=5200)
    trace.add("llm_response", text_preview="Reduzi as falhas de 3 para 2.", meta={"usage": {"output_tokens": 240}})
    trace.tool("read_file", {"path": "src/api.py", "limit": 80}, result="def criar_pedido(payload): ...")
    normal = pad_normal(trace, trace.events[start:])

    loop_start = len(trace.events)
    same_failure = "1 failed: test_cria_pedido — AssertionError: status 422 != 201"
    for variant in range(12):
        trace.tool(
            "terminal",
            {"command": f"pytest tests/test_api.py::test_cria_pedido -q --seed {variant}"},
            status="error",
            result=same_failure,
            duration_ms=4800 + variant * 30,
        )
    trace.add("assistant_message", text_preview="Vou rodar o teste novamente para confirmar o comportamento.")
    trace.add(
        "budget_exhausted",
        status="error",
        error_message="max_iterations_reached(60/60)",
        meta={"turn_exit_reason": "max_iterations_reached(60/60)"},
    )
    trace.add("run_end", status="failed", error_message="max_iterations_reached(60/60)", meta={"completed": False, "turn_exit_reason": "max_iterations_reached(60/60)"})
    loop = trace.events[loop_start:]

    fix_start = len(trace.events)
    trace.add("steer", text_preview="Pare de repetir o teste: leia a mensagem de erro completa e valide o payload antes de rodar de novo.", meta={"queued": True})
    trace.tool("terminal", {"command": "pytest tests/test_api.py::test_cria_pedido -q 2>&1 | tail -40"}, status="error", result="AssertionError: campo 'cliente_id' ausente no payload", duration_ms=4700)
    trace.tool("read_file", {"path": "src/validators.py", "limit": 60}, result="def validar_payload(payload): ...")
    trace.tool("edit_file", {"path": "src/validators.py", "patch": "@@ exige cliente_id"}, result="patch aplicado em validar_payload")
    trace.tool("terminal", {"command": "pytest tests/test_api.py -q"}, result="15 passed", duration_ms=5100)
    trace.add("llm_response", text_preview="A causa era `cliente_id` ausente na validação.", meta={"usage": {"output_tokens": 260}})
    trace.add("assistant_message", text_preview="Suíte verde: 15 testes passando.")
    trace.add("turn_end", status="ok")
    trace.add("run_end", status="completed", meta={"completed": True})
    correction_events = trace.events[fix_start:]

    return write_scenario(
        root,
        scenario_id="s2_budget_exhausted",
        title="Orçamento de iterações esgotado com resultados idênticos",
        failure_mode="Repetição de comando com resultado invariável até o harness encerrar o turno por `max_iterations_reached(60/60)`.",
        description=(
            "O agente roda o mesmo teste com sementes diferentes, mas a mensagem de falha é sempre a mesma. "
            "Sem leitura do erro completo, nenhuma ação muda o estado, e o turno termina sem resposta final."
        ),
        reference="https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration",
        expected_rules=["loop.no_progress", "failure.budget_exhausted"],
        expected_verdict="failure",
        correction={
            "strategy": "steer + leitura do erro completo",
            "description": "Orientação para ler a mensagem de erro integral antes de repetir o comando; a causa real (`cliente_id` ausente) aparece e o teste passa.",
        },
        phases=[
            {"phase": "saudável", "label": "normal", "loop_type": "none", "events": normal},
            {"phase": "loop", "label": "loop", "loop_type": "no_progress", "events": loop},
            {"phase": "correção", "label": "normal", "loop_type": "none", "events": correction_events},
        ],
    )


# ---------------------------------------------------------------------------
# Cenário 3 — lock de escrita no state.db
# ---------------------------------------------------------------------------
def scenario_state_db_lock(root: Path) -> Dict[str, Any]:
    session = "sess_s3_state_db_lock"
    trace = Trace(session)

    start = len(trace.events)
    trace.turn("Resuma as últimas decisões do projeto.", "Vou buscar no histórico da sessão.")
    trace.tool("session_search", {"query": "decisões de arquitetura"}, result="5 resultados")
    trace.tool("read_file", {"path": "docs/decisoes.md", "limit": 150}, result="ADR-001 … ADR-007")
    trace.add("llm_response", text_preview="Resumo: três decisões mudaram o escopo.", meta={"usage": {"output_tokens": 320}})
    trace.add("assistant_message", text_preview="Resumo das decisões das últimas semanas.")
    trace.add("compact", text_preview="compactando contexto de 84k tokens", meta={"context_tokens": 84120})
    trace.tool("read_file", {"path": "docs/decisoes.md", "offset": 150, "limit": 80}, result="ADR-008 … ADR-010")
    trace.tool("edit_file", {"path": "docs/resumo-semanal.md", "patch": "@@ adiciona resumo"}, result="resumo semanal atualizado")
    trace.tool("terminal", {"command": "git log --oneline -5"}, result="5 commits recentes")
    trace.add("llm_response", text_preview="Resumo consolidado em docs/resumo-semanal.md.", meta={"usage": {"output_tokens": 280}})
    normal = pad_normal(trace, trace.events[start:])

    fail_start = len(trace.events)
    for attempt in range(12):
        trace.add(
            "persistence_failure",
            status="error",
            error_type="persistence_locked",
            error_message="session_persistence_failed:locked — database is locked (state.db-shm byte 120, WAL write lock)",
            meta={"attempt": attempt + 1, "patience_s": 60},
        )
        trace.add(
            "api_error",
            status="error",
            error_type="retryable",
            error_message="turn abortado por falha de persistência; reexecutando gravação do transcript (tentativa %d)" % (attempt + 1),
        )
    trace.add("assistant_message", text_preview="A gravação do transcript continua falhando; o trabalho está pronto mas não persiste.")
    trace.add("run_end", status="failed", error_message="session_persistence_failed:locked", meta={"completed": False})
    failure_events = trace.events[fail_start:]

    fix_start = len(trace.events)
    trace.add("interrupt", text_preview="turno interrompido pelo usuário após erro persistente")
    trace.tool("terminal", {"command": "python -m hermes_state_lockowners"}, result="PID 594094 (hermes --worktree --yolo) holds WAL write lock on state.db-shm")
    trace.add("assistant_message", text_preview="Diagnóstico: um subagente em worktree mantinha o lock de escrita.")
    trace.tool("terminal", {"command": "hermes sessions recover --dry-run"}, result="4 tabelas verificáveis; nenhuma linha órfã")
    trace.tool("read_file", {"path": "docs/decisoes.md", "limit": 60}, result="ADR-001 … ADR-003")
    trace.tool("read_file", {"path": "docs/resumo-semanal.md", "limit": 40}, result="resumo semanal preservado…")
    trace.add("assistant_message", text_preview="Sessão recuperada; resumo semanal intacto.")
    trace.add("llm_response", text_preview="Sessão retomada após liberar o lock concorrente.", meta={"usage": {"output_tokens": 290}})
    trace.add("assistant_message", text_preview="Resumo entregue após recuperação da sessão.")
    trace.add("turn_end", status="ok")
    trace.add("run_end", status="completed", meta={"completed": True})
    correction_events = trace.events[fix_start:]

    return write_scenario(
        root,
        scenario_id="s3_state_db_lock",
        title="Lock de escrita no state.db durante gravação de transcript",
        failure_mode="`session_persistence_failed:locked` aborta o turno quando gateway, CLI e subagente disputam a escrita do WAL.",
        description=(
            "Quatro tentativas de gravar o transcript falham com `database is locked`. O turno é abortado com "
            "erro, apesar de o trabalho do modelo ter sido concluído. A recuperação exige identificar o processo "
            "que detém o lock — não repetir a escrita."
        ),
        reference="https://hermes-agent.nousresearch.com/docs/developer-guide/session-storage",
        expected_rules=["failure.persistence_locked", "failure.retry_storm"],
        expected_verdict="failure",
        correction={
            "strategy": "interrupção + diagnóstico de lock",
            "description": "`hermes_state_lockowners` identifica o processo concorrente; a sessão é retomada após a liberação do lock.",
        },
        phases=[
            {"phase": "saudável", "label": "normal", "loop_type": "none", "events": normal},
            {"phase": "falha", "label": "failure", "loop_type": "persistence_locked", "events": failure_events},
            {"phase": "correção", "label": "normal", "loop_type": "none", "events": correction_events},
        ],
    )


# ---------------------------------------------------------------------------
# Cenário 4 — tempestade de retries do provedor
# ---------------------------------------------------------------------------
def scenario_retry_storm(root: Path) -> Dict[str, Any]:
    session = "sess_s4_retry_storm"
    trace = Trace(session)

    start = len(trace.events)
    trace.turn("Explique o trade-off entre Modal e Daytona para execução persistente.", "Vou consultar a documentação de backends.")
    trace.tool("web_search", {"query": "hermes agent terminal backend modal daytona"}, result="8 resultados")
    trace.tool("web_extract", {"url": "https://hermes-agent.nousresearch.com/docs/user-guide/features/tools"}, result="comparativo de backends…")
    trace.add("llm_response", text_preview="Modal e Daytona hibernam por ociosidade.", meta={"usage": {"output_tokens": 350}})
    trace.add("assistant_message", text_preview="Resumo comparativo dos backends serverless.")
    trace.tool("read_file", {"path": "docs/backends.md", "limit": 100}, result="tabela de backends…")
    trace.tool("web_search", {"query": "daytona vs modal cold start latency"}, result="6 resultados")
    trace.tool("read_file", {"path": "docs/backends.md", "offset": 100, "limit": 80}, result="notas de hibernação…")
    normal = pad_normal(trace, trace.events[start:])

    fail_start = len(trace.events)
    for attempt in range(12):
        status_code = 429 if attempt % 2 == 0 else 503
        trace.add(
            "api_error",
            status="error",
            error_type="provider_rate_limit" if status_code == 429 else "provider_server_error",
            error_message=f"api_request_error status={status_code} retryable=true retry_count={attempt + 1} source=provider",
            meta={"status_code": status_code, "retry_count": attempt + 1, "max_retries": 8, "api_call_count": attempt + 1},
        )
    trace.add("assistant_message", text_preview="O provedor segue instável; vou tentar mais uma vez antes de desistir.")
    for attempt in range(12, 24):
        status_code = 429 if attempt % 2 == 0 else 503
        trace.add(
            "api_error",
            status="error",
            error_type="provider_rate_limit" if status_code == 429 else "provider_server_error",
            error_message=f"api_request_error status={status_code} retryable=true retry_count={attempt + 1} source=provider",
            meta={"status_code": status_code, "retry_count": attempt + 1, "max_retries": 8, "api_call_count": attempt + 1},
        )
    trace.add("run_end", status="failed", error_message="8 tentativas de provedor esgotadas", meta={"completed": False, "partial": True})
    failure_events = trace.events[fail_start:]

    fix_start = len(trace.events)
    trace.add("steer", text_preview="Troque o provedor e continue de onde parou.", meta={"queued": True})
    trace.tool("web_search", {"query": "hermes agent provider failover configuration"}, result="5 resultados")
    trace.tool("read_file", {"path": "docs/providers.md", "limit": 90}, result="ordem de failover e limites de retry…")
    trace.tool("web_extract", {"url": "https://hermes-agent.nousresearch.com/docs/user-guide/features/tools"}, result="seção de failover de provedores…")
    trace.tool("read_file", {"path": "docs/providers.md", "offset": 90, "limit": 60}, result="exemplo de config de failover…")
    trace.add("assistant_message", text_preview="Configuração de failover revisada antes de aplicar.")
    trace.add("llm_response", text_preview="Configurei failover e limitei retries a 3.", meta={"usage": {"output_tokens": 300}})
    trace.add("assistant_message", text_preview="Trade-off explicado; failover configurado para evitar a tempestade.")
    trace.add("turn_end", status="ok")
    trace.add("run_end", status="completed", meta={"completed": True})
    correction_events = trace.events[fix_start:]

    return write_scenario(
        root,
        scenario_id="s4_provider_retry_storm",
        title="Tempestade de retries do provedor",
        failure_mode="Erros `api_request_error` 429/503 em rajada: o agente consome o orçamento tentando de novo em vez de trocar de rota.",
        description=(
            "Sete tentativas consecutivas contra um provedor instável, sem backoff eficaz nem failover. "
            "A execução termina `failed` com resposta parcial, apesar de o trabalho ser recuperável."
        ),
        reference="https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks (hook `api_request_error`)",
        expected_rules=["failure.retry_storm", "failure.run_not_completed"],
        expected_verdict="failure",
        correction={
            "strategy": "steer + failover de provedor",
            "description": "Orientação para trocar de provedor e limitar retries; a execução é retomada e concluída.",
        },
        phases=[
            {"phase": "saudável", "label": "normal", "loop_type": "none", "events": normal},
            {"phase": "falha", "label": "failure", "loop_type": "retry_storm", "events": failure_events},
            {"phase": "correção", "label": "normal", "loop_type": "none", "events": correction_events},
        ],
    )


# ---------------------------------------------------------------------------
# Cenário 5 — ciclo de ferramentas sem convergência
# ---------------------------------------------------------------------------
def scenario_tool_cycle(root: Path) -> Dict[str, Any]:
    session = "sess_s5_tool_cycle"
    trace = Trace(session)

    start = len(trace.events)
    trace.turn("Descubra qual biblioteca de filas o projeto usa hoje.", "Vou procurar no repositório e nas dependências.")
    trace.tool("terminal", {"command": "grep -rn 'import' pyproject.toml"}, result="dependências principais listadas")
    trace.tool("read_file", {"path": "pyproject.toml", "limit": 120}, result="[project] dependencies = [...]")
    trace.tool("edit_file", {"path": "docs/stack.md", "patch": "@@ registra decisão"}, result="patch aplicado")
    trace.add("llm_response", text_preview="A fila é implementada com a biblioteca interna `fila_br`.", meta={"usage": {"output_tokens": 260}})
    trace.add("assistant_message", text_preview="Encontrei a biblioteca de filas usada hoje.")
    trace.tool("terminal", {"command": "ls src/queue"}, result="fila_br.py  __init__.py")
    normal = pad_normal(trace, trace.events[start:])

    loop_start = len(trace.events)
    for round_number in range(12):
        trace.tool(
            "web_search",
            {"query": f"biblioteca de filas python usada em projetos brasileiros página {round_number + 1}"},
            result=f"resultados de busca (página {round_number + 1}): 10 itens, nenhum definitivo",
            duration_ms=900 + round_number * 20,
        )
        trace.tool(
            "web_extract",
            {"url": f"https://exemplo.org/filas/pagina-{round_number + 1}"},
            result=f"conteúdo da página {round_number + 1}: comparação genérica, sem menção ao projeto",
            duration_ms=700 + round_number * 15,
        )
    trace.add("assistant_message", text_preview="Vou continuar buscando para confirmar a biblioteca.")

    loop = trace.events[loop_start:]

    fix_start = len(trace.events)
    trace.add("steer", text_preview="A resposta já está em pyproject.toml e src/queue. Pare de buscar na web e responda com o que leu.", meta={"queued": True})
    trace.tool("read_file", {"path": "src/queue/fila_br.py", "limit": 80}, result="class FilaBR: usa redis + threads")
    trace.tool("terminal", {"command": "grep -rn 'fila_br' src/ | head"}, result="12 usos de fila_br em src/")
    trace.tool("read_file", {"path": "src/queue/__init__.py", "limit": 40}, result="from .fila_br import FilaBR")
    trace.add("llm_response", text_preview="A biblioteca é interna: `fila_br`, sobre Redis.", meta={"usage": {"output_tokens": 200}})
    trace.add("assistant_message", text_preview="A fila vem de `fila_br` (interna) sobre Redis, conforme src/queue.")
    trace.add("turn_end", status="ok")
    trace.add("run_end", status="completed", meta={"completed": True})
    trace.tool("read_file", {"path": "docs/stack.md", "limit": 40}, result="stack documentada…")
    trace.add("session_end", status="ok", meta={"end_reason": "user_exit"})
    correction_events = trace.events[fix_start:]

    return write_scenario(
        root,
        scenario_id="s5_tool_cycle",
        title="Ciclo busca→extração sem convergência",
        failure_mode="Alternância `web_search` → `web_extract` com argumentos sempre diferentes: nenhuma repetição idêntica, mas nenhum progresso.",
        description=(
            "Nove voltas do ciclo de duas ferramentas. Um detector de repetição exata não acusa nada — "
            "só um detector de ciclo captura o padrão. A resposta já existia no próprio repositório."
        ),
        reference="https://github.com/NousResearch/hermes-agent/issues?q=tool+loop",
        expected_rules=["loop.cycle"],
        expected_verdict="loop",
        correction={
            "strategy": "steer + âncora no estado local",
            "description": "Orientação para usar o que já foi lido no repositório; a resposta é produzida sem novas buscas.",
        },
        phases=[
            {"phase": "saudável", "label": "normal", "loop_type": "none", "events": normal},
            {"phase": "ciclo", "label": "loop", "loop_type": "cycle", "events": loop},
            {"phase": "correção", "label": "normal", "loop_type": "none", "events": correction_events},
        ],
    )


SCENARIOS = [
    scenario_read_file_dedup,
    scenario_budget_exhausted,
    scenario_state_db_lock,
    scenario_retry_storm,
    scenario_tool_cycle,
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Gera os cenários reproduzíveis do hermes-apprentice")
    parser.add_argument("--out", default="scenarios", help="diretório de saída")
    args = parser.parse_args()

    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)
    manifest: List[Dict[str, Any]] = []
    for builder in SCENARIOS:
        meta = builder(root)
        manifest.append(meta)
        print(
            f"[ok] {meta['id']}: {meta['event_count']} eventos, "
            f"{len(meta['label_zones'])} zonas de rótulo, regras esperadas {meta['expected_rules']}"
        )

    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "scenario_count": len(manifest),
                "window_size": WINDOW_SIZE,
                "scenarios": manifest,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[ok] manifesto escrito em {root / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
