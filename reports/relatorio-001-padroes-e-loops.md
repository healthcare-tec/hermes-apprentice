# Relatório nº 1 — Padrões e loops detectados

*Gerado em 2026-10-10 01:37 UTC por `hermes-apprentice` — dados brutos em `artifacts/`.*

## 1. Resumo executivo

Foram analisados **12 achados** em **5 sessões** sintéticas derivadas de modos de falha reais do harness Hermes. Os detectores determinísticos identificaram **5 loops** e **7 falhas**.

O modelo local **Qwen3-0.6B** (CPU, sem GPU) alcançou acurácia `0.7` e F1 macro `0.552`, contra `1.0` / `1.0` dos detectores determinísticos. A leitura desses números está na seção 6.

## 2. Versão e configuração do Hermes identificadas

| Item | Valor |
|---|---|
| Versão do CLI (`hermes --version`) | Hermes Agent v0.19.0 (2026.7.20) · Install directory: /home/ubuntu/hermes-lab/venv/lib/python3.12/site-packages · Install method: pip · Python: 3.12.3 · OpenAI SDK: 2.24.0 · Up to date |
| Versão do pacote (metadados) | 0.19.0 |
| Última release upstream (GitHub) | v0.21.6 |
| HERMES_HOME resolvido | /home/ubuntu/hermes-lab/home (regra: explicit) |
| state.db presente | False |
| Perfis nomeados | 0 |

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

![Achados por regra](achados-por-regra.png)

![Matriz de confusão do modelo](matriz-confusao-modelo.png)

## 6. Desempenho inicial do Qwen3-0.6B

Conjunto de avaliação: **30 janelas** ({'failure': 6, 'loop': 9, 'normal': 15}), piso de **12** eventos por janela.

| Métrica | Detectores determinísticos | Qwen3-0.6B |
|---|---|---|
| Acurácia | 1.0 | 0.7 |
| F1 macro | 1.0 | 0.552 |
| F1 `normal` | 1.0 | 0.8 |
| F1 `loop` | 1.0 | 0.0 |
| F1 `failure` | 1.0 | 0.857 |
| Latência média/janela (s) | 7e-05 | 8.532 |
| JSON inválido | — | 10 |

- Latência: média **8.532 s**, mediana **8.495 s**, p95 **9.696 s**.
- Vazão: **5.3 tokens/s** (mín 4.7, máx 6.2).
- Ambiente: 6 núcleos, 23.85 GB de RAM, dispositivo `cpu`, dtype `float32`, carga dos pesos 5.7 s.

![F1 por classe](modelo-x-regras.png)

![Latência por janela](latencia-qwen.png)

### 6.1 Erros do modelo (amostra)

| Cenário | Verdadeiro | Previsto | Tipo | Motivo declarado |
|---|---|---|---|---|
| s1_loop_read_file_dedup | loop | normal | repetition | The agent reads the same file multiple times with no change in content |
| s1_loop_read_file_dedup | loop | normal | repetition | The agent reads the same file multiple times with no change in content |
| s1_loop_read_file_dedup | loop | normal | repetition | The agent reads the same file multiple times with no change in content |
| s2_budget_exhausted | loop | failure | none | The agent repeatedly calls the same tool with the same result, leading |
| s2_budget_exhausted | loop | unknown | none |  |
| s2_budget_exhausted | loop | failure | none | The agent repeatedly calls the same tool with the same result, leading |
| s5_tool_cycle | loop | unknown | none |  |
| s5_tool_cycle | loop | normal | none | The agent is performing web extraction and search tasks repeatedly wit |
| s5_tool_cycle | loop | unknown | none |  |
| s1_loop_read_file_dedup | normal | unknown | none |  |

## 7. Leitura dos resultados e limites

- Os cenários são **sintéticos e rotulados por construção**. Eles reproduzem a assinatura de eventos de falhas documentadas no harness, não sessões reais de usuários. Acurácia alta aqui prova que a regra captura a assinatura; não prova generalização.
- O coletor foi validado contra um `HERMES_HOME` real do Hermes Agent vv0.19.0 instalado em ambiente isolado, mas as sessões analisadas neste relatório são de fixture.
- O Qwen3-0.6B é o piso da escala. A comparação da seção 6 é o argumento para tratar o modelo como *segunda opinião* sobre janelas ambíguas, e as regras explícitas como o detector principal.
- Todo texto observado passa por redação de segredos (`redact.py`) antes de ser gravado.

## 8. Próximos passos

1. Coletar sessões reais de um Hermes em uso (com consentimento) para medir falsos positivos das regras.
2. Adicionar a assinatura de *stub* de deduplicação como evento de primeira classe quando o harness expuser o motivo.
3. Testar Qwen3-1.7B/4B na mesma bancada — a hipótese é que a curva de F1 por parâmetro seja o dado mais útil.
4. Transformar os achados em sugestões acionáveis (ex.: *steer* recomendado, compressão de contexto, troca de modelo).

---

*Reproduza este relatório com `python -m hermes_apprentice all` — ver `README.md`.*