# Cenários reproduzíveis

Cinco cenários, cada um com três fases — **saudável**, **falha/loop** e **correção**. A fase de
correção existe porque a pergunta operacionalmente útil não é "o agente travou?", mas "o que tirou o
agente do laço?".

## Formato

```
scenarios/<id>/
├── scenario.json   # metadados, regras esperadas, correção, zonas de rótulo
└── events.ndjson   # um evento normalizado por linha
```

`label_zones` declara faixas de índices com o rótulo esperado. O validador
(`python scripts/make_scenarios.py`) e os testes conferem que os detectores reproduzem as regras
esperadas em cada cenário — se um cenário deixar de ser um exemplo válido do seu modo de falha, o
teste falha.

## Os cinco

### 1. `s1_loop_read_file_dedup` — loop de `read_file` com *stub* de deduplicação

Depois de uma edição bem-sucedida, o agente volta a chamar `read_file` com os mesmos argumentos. O
harness responde com o mesmo *stub* ("arquivo já lido nesta sessão; conteúdo idêntico ao anterior").
Nada no mundo mudou, mas o modelo interpreta a resposta como informação nova e tenta de novo.

- **Assinatura**: mesma ferramenta, mesmos argumentos, mesmo resultado — `read_file` ×12.
- **Regras**: `loop.repetition`, `loop.no_progress`.
- **Correção**: `steer` proibindo a releitura e indicando leitura paginada (`offset`).
- **Referência**: [hermes-agent#15759](https://github.com/NousResearch/hermes-agent/issues/15759).

### 2. `s2_budget_exhausted` — orçamento de iterações esgotado

O agente roda o mesmo teste com sementes diferentes; a mensagem de falha é idêntica
(`1 failed: test_cria_pedido — AssertionError: status 422 != 201`). O turno termina com
`max_iterations_reached(60/60)` e a execução é marcada `failed`.

- **Assinatura**: argumentos variam, resultado invariável; encerramento por orçamento.
- **Regras**: `loop.no_progress`, `failure.budget_exhausted`.
- **Correção**: `steer` + ler a mensagem de erro completa (a causa era `cliente_id` ausente).
- **Referência**: [Programmatic Integration](https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration) (`turn_exit_reason`).

### 3. `s3_state_db_lock` — lock de escrita no `state.db`

Seis tentativas de gravar o transcript falham com `session_persistence_failed:locked`. O trabalho do
modelo terminou, mas o turno é abortado. Repetir a escrita **piora** a contenção.

- **Assinatura**: `persistence_failure` repetido com mensagem idêntica + `api_error` de retry.
- **Regras**: `failure.persistence_locked`, `failure.retry_storm`.
- **Correção**: interromper e identificar quem detém o lock (`hermes_state_lockowners`).
- **Referência**: [Session Storage](https://hermes-agent.nousresearch.com/docs/developer-guide/session-storage).

### 4. `s4_provider_retry_storm` — tempestade de retries

Vinte e quatro `api_request_error` (429/503) em sequência. O agente consome o orçamento tentando de
novo em vez de trocar de rota; a execução termina `failed` com resposta parcial.

- **Assinatura**: rajada de erros de provedor com `retry_count` crescente.
- **Regras**: `failure.retry_storm`, `failure.run_not_completed`.
- **Correção**: `steer` + failover de provedor com limite de retries.
- **Referência**: hook [`api_request_error`](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks).

### 5. `s5_tool_cycle` — ciclo busca→extração sem convergência

Doze voltas de `web_search` → `web_extract`. **Nenhuma chamada se repete de forma idêntica** — um
detector de repetição exata não acusa nada. A resposta já estava em `pyproject.toml` e `src/queue/`.

- **Assinatura**: alternância de período 2 com argumentos sempre diferentes.
- **Regras**: `loop.cycle`.
- **Correção**: `steer` ancorando o agente no estado local já lido.
- **Referência**: categoria de "tool loop" nas issues do harness.

## Por que sintéticos

Gerar essas falhas em um Hermes real exigiria provocar contenção de WAL, derrubar um provedor e
induzir *stubs* de deduplicação de forma determinística — e cada execução dependeria de um modelo
externo, tornando o conjunto não reprodutível e não auditável. Os cenários aqui são **fiéis à
assinatura** (nomes de erro, campos e sequências retirados da documentação e das issues do harness),
mas são dados sintéticos rotulados por construção.

A consequência está declarada no relatório: a acurácia dos detectores neste conjunto **não** mede
generalização. O que ela mede é: as regras capturam as assinaturas que dizem capturar.

## Como estender

1. Adicione uma função `scenario_*` em `scripts/make_scenarios.py`.
2. Registre-a em `SCENARIOS`.
3. Rode `python scripts/make_scenarios.py --out scenarios` e depois `pytest -q`.

O teste `test_scenarios_emit_expected_rules` falha se as regras esperadas não forem detectadas,
garantindo que um cenário novo só entra se for um exemplo válido do modo de falha declarado.