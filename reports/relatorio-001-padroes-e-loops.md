# Relatório nº 1 — Padrões e loops detectados

*Gerado em 2026-10-10 01:58 UTC por `hermes-apprentice` — dados brutos em `artifacts/`.*

## 1. Resumo executivo

Foram analisados **12 achados** em **5 sessões** derivadas de modos de falha documentados do harness Hermes. Os detectores determinísticos identificaram **5 loops** e **7 falhas**, e reproduziram as regras esperadas em **5 de 5** cenários.

O modelo local **Qwen3-0.6B** (CPU, sem GPU) alcançou acurácia `0.7` sobre **todas** as 30 janelas — respostas ilegíveis contam como erro, sem serem descartadas do denominador — e F1 macro `0.544`, contra `1.0` / `1.0` dos detectores determinísticos. A leitura desses números — e o que fazer com eles — está na seção *Desempenho inicial do Qwen3-0.6B*.

## 2. Versão e configuração do Hermes identificadas

| Item | Valor |
|---|---|
| Versão do CLI (`hermes --version`) | Hermes Agent v0.19.0 (2026.7.20) · Install directory: /home/ubuntu/hermes-lab/venv/lib/python3.12/site-packages · Install method: pip · Python: 3.12.3 · OpenAI SDK: 2.24.0 · Up to date |
| Versão do pacote (metadados) | 0.19.0 |
| Origem dos metadados | hermes_binary_environment |
| Última release upstream (GitHub) | v0.21.6 |
| HERMES_HOME resolvido | examples/hermes-home-fixture (regra: explicit) |
| `config.yaml` | presente=True, chaves=4 |
| `state.db` | presente=True, sessões=1, mensagens=11 |
| Perfis nomeados | 0 |

> A chamada `hermes --version` foi executada com HERMES_HOME temporário: o CLI do harness grava cache no HERMES_HOME, e apontá-lo para o diretório real violaria a garantia de somente observação.
> Divergência de versões: pacote PyPI 0.19.0 x última release do GitHub v0.21.6.

## 3. Método: como os eventos são coletados

A coleta é **somente observação**. Nenhum módulo escreve em `state.db`, em `config.yaml` ou em qualquer artefato do harness: a conexão SQLite é aberta com `file:...?mode=ro` **e** `PRAGMA query_only = ON`, e os testes provam que uma escrita é recusada (`assert_readonly`). As fontes, em ordem de preferência:

| Fonte | Transporte | Papel |
|---|---|---|
| `runs_api` | SSE `GET /v1/runs/{id}/events` | Contrato publicado; caminho preferido para execuções ao vivo |
| `spool` | NDJSON escrito por hook de observação | Opt-in explícito; hook não devolve diretiva nenhuma |
| `state_db` | SQLite somente leitura | Fonte da verdade depois do fato |
| `logs` | Tail de `~/.hermes/logs/*.log` | Cobre o que não chega às outras fontes |
| `fixture` | NDJSON normalizado | Cenários reproduzíveis e testes |

## 4. Cenários reproduzíveis de falha e correção

| Cenário | Modo de falha observado | Regra esperada | Correção aplicada |
|---|---|---|---|
| s1_loop_read_file_dedup | Releitura infinita do mesmo arquivo: o harness devolve um stub de deduplicação e o modelo interpreta como resultado novo. | loop.repetition, loop.no_progress | steer |
| s2_budget_exhausted | Repetição de comando com resultado invariável até o harness encerrar o turno por `max_iterations_reached(60/60)`. | loop.no_progress, failure.budget_exhausted | steer + leitura do erro completo |
| s3_state_db_lock | `session_persistence_failed:locked` aborta o turno quando gateway, CLI e subagente disputam a escrita do WAL. | failure.persistence_locked, failure.retry_storm | interrupção + diagnóstico de lock |
| s4_provider_retry_storm | Erros `api_request_error` 429/503 em rajada: o agente consome o orçamento tentando de novo em vez de trocar de rota. | failure.retry_storm, failure.run_not_completed | steer + failover de provedor |
| s5_tool_cycle | Alternância `web_search` → `web_extract` com argumentos sempre diferentes: nenhuma repetição idêntica, mas nenhum progresso. | loop.cycle | steer + âncora no estado local |

## 5. Padrões e loops detectados

| Padrão | Regra | Ocorrências |
|---|---|---|
| Execução encerrada sem resposta final | failure.run_not_completed | 3 |
| Loop por repetição de chamada | loop.repetition | 2 |
| Loop sem progresso (stub de deduplicação) | loop.no_progress | 2 |
| Tempestade de retries do provedor | failure.retry_storm | 2 |
| Orçamento de iterações esgotado | failure.budget_exhausted | 1 |
| Lock de escrita no state.db | failure.persistence_locked | 1 |
| Ciclo de ferramentas | loop.cycle | 1 |

Conferência cenário a cenário:

| Cenário | Veredito esperado | Veredito obtido | Regras | Regras detectadas |
|---|---|---|---|---|
| s1_loop_read_file_dedup | loop | loop | ✅ | loop.no_progress, loop.repetition |
| s2_budget_exhausted | failure | failure | ✅ | failure.budget_exhausted, failure.run_not_completed, loop.no_progress, loop.repetition |
| s3_state_db_lock | failure | failure | ✅ | failure.persistence_locked, failure.retry_storm, failure.run_not_completed |
| s4_provider_retry_storm | failure | failure | ✅ | failure.retry_storm, failure.run_not_completed |
| s5_tool_cycle | loop | loop | ✅ | loop.cycle |

![Achados por regra](achados-por-regra.png)

## 6. Desempenho inicial do Qwen3-0.6B

Conjunto de avaliação: **30 janelas** rotuladas ({'failure': 6, 'loop': 9, 'normal': 15}), com piso de **12** eventos por janela e passo de **6**.

| Métrica | Detectores determinísticos | Qwen3-0.6B |
|---|---|---|
| Acurácia (todas as janelas) | 1.0 | 0.7 |
| Acurácia (só respostas legíveis) | 1.0 | 0.7 |
| F1 macro | 1.0 | 0.544 |
| F1 `normal` | 1.0 | 0.833 |
| F1 `loop` | 1.0 | 0.0 |
| F1 `failure` | 1.0 | 0.8 |
| Latência média/janela (s) | 0.00012 | 9.701 |
| Respostas ilegíveis | — | 0 |

- Latência: média **9.701 s**, mediana **9.584 s**, p95 **11.176 s**, máxima **12.667 s**.
- Vazão: **4.8 tokens/s** (mín 3.8, máx 5.8), com **521.1** tokens de prompt por janela.
- Ambiente: 6 núcleos, 23.85 GB de RAM disponíveis, dispositivo `cpu`, dtype `float32`, carga dos pesos 2.97 s, sem GPU.

- **Formato da resposta:** 25 de 30 janelas saíram como JSON estrito, 0 precisaram de reparo (quebras de linha literais dentro das strings, que o JSON padrão recusa) e 5 foram recuperadas de um objeto cortado no meio pela janela de geração. Nenhuma janela ficou ilegível, então os números abaixo medem **julgamento**, não capacidade de obedecer ao formato pedido.
- **Classes que o modelo nunca previu:** `loop`. Precisão zero aqui não vem de confundir exemplos, vem de o modelo ter colapsado para as classes majoritárias (`normal` e `failure`). É o comportamento esperado de um modelo de 0,6 B sem calibração de rótulos — e é a razão pela qual as regras explícitas continuam sendo o detector.
- **Menor recall:** `loop` = 0.0. Recall baixo significa que a classe *existe* na janela e passa despercebida — operacionalmente é o pior tipo de erro, porque a falha segue adiante sem alarme.

![F1 por classe](modelo-x-regras.png)

![Latência por janela](latencia-qwen.png)

![Matriz de confusão do modelo](matriz-confusao-modelo.png)

**Erros do modelo** (9 no total; amostra de 10)

| Cenário | Verdadeiro | Previsto | Tipo declarado | Motivo declarado |
|---|---|---|---|---|
| s1_loop_read_file_dedup | loop | normal | repetition | The agent reads the same file multiple times with no change in content |
| s1_loop_read_file_dedup | loop | normal | repetition | The agent reads the same file multiple times with no change in content |
| s1_loop_read_file_dedup | loop | normal | repetition | The agent reads the same file multiple times with no change in content |
| s2_budget_exhausted | loop | failure | none | The agent repeatedly calls the same tool with the same result, leading |
| s2_budget_exhausted | loop | failure | retry_storm | The agent repeatedly calls the same tool with the same result, causing |
| s2_budget_exhausted | loop | failure | none | The agent repeatedly calls the same tool with the same result, leading |
| s5_tool_cycle | loop | normal | none | The agent is performing web searches and extracting content from multi |
| s5_tool_cycle | loop | normal | none | The agent is performing web extraction and search tasks repeatedly wit |
| s5_tool_cycle | loop | normal | none | The agent is performing web searches and extracting content repeatedly |

## 7. Leitura dos resultados e limites

- Os cenários são **sintéticos e rotulados por construção**. Eles reproduzem a assinatura de eventos de falhas documentadas no harness, não sessões reais de usuários. Acurácia de 1,0 dos detectores aqui mede fidelidade da regra à assinatura — **não** generalização, e não mede falsos positivos em tráfego real.
- O coletor foi validado contra um `state.db` gravado pela API oficial do próprio harness (v0.19.0) e preservado em `examples/hermes-home-fixture/`; as sessões avaliadas neste relatório são as de `scenarios/`.
- O Qwen3-0.6B é o piso da escala de modelos locais. A comparação da seção anterior é o argumento para tratá-lo como *segunda opinião* sobre janelas ambíguas, e as regras explícitas como o detector principal.
- Todo texto observado passa por redação de segredos (`redact.py`) antes de ser gravado; a redação cobre formatos conhecidos e não substitui revisão humana antes de publicar dados.

## 8. Próximos passos

1. Coletar sessões reais de um Hermes em uso (com consentimento) para medir falsos positivos das regras.
2. Tratar a falha de formato do modelo antes de trocar de modelo: few-shot com um exemplo exato de JSON e um parser tolerante.
3. Testar Qwen3-1.7B/4B na mesma bancada — a hipótese é que a curva de F1 por parâmetro seja o dado mais útil.
4. Transformar os achados em sugestões acionáveis (ex.: *steer* recomendado, compressão de contexto, troca de modelo).

---

*Reproduza este relatório com `python -m hermes_apprentice all` — ver `README.md`.*