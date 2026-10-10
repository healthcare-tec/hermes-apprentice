# Arquitetura

## Princípio

O `hermes-apprentice` é um **observador externo**. Ele não é um plugin do harness, não participa do
ciclo do agente e não tem permissão de escrita sobre nada que o Hermes produza. Essa escolha é o que
torna o instrumento seguro para rodar ao lado de um agente em uso real.

## Fluxo de dados

```
                      ┌──────────────────────────────────────────┐
   fonte             │            normalização                  │      consumidores
─────────────────────┼──────────────────────────────────────────┼───────────────────────
 state.db (SQLite)   │  PRAGMA table_info(tabela) → mapeamento   │  detectors/engine.py
 logs/*.log          │  tolerante a colunas ausentes             │      ↓
 Runs API (SSE)      │           ↓                               │  findings (JSON)
 spool (hook NDJSON) │   schema.Event  (NDJSON, 1 linha/evento)  │      ↓
 fixtures            │           ↓                               │  model/*  (Qwen3-0.6B)
─────────────────────┴──────────────────────────────────────────┴───────────────────────
```

O `schema.Event` é o contrato único. Quem adiciona uma fonte só precisa produzir `Event`; quem
adiciona um detector só precisa consumir `Event`. Nenhum detector sabe de onde o evento veio — o
campo `source` existe para auditoria, não para ramificar lógica.

## Por que um esquema próprio em vez de usar os tipos do harness

Os nomes de evento do harness (`run.started`, `tool.completed`, `assistant.delta`) são o contrato de
**uma** superfície — a Runs API. O `state.db` e os logs não produzem os mesmos nomes, e um hook
produz um terceiro vocabulário. Normalizar cedo permite:

1. comparar as fontes num mesmo eixo temporal (deduplicação por cinco campos dimensionais);
2. escrever detectores que não dependem da versão da superfície de eventos;
3. gravar fixtures estáveis que continuam válidos quando o harness renomeia um evento.

O mapeamento de cada superfície para o vocabulário interno está explícito em
`collector/runs_api.py` (`SSE_EVENT_MAP`) e `collector/spool.py` (`HOOK_EVENT_MAP`) — são tabelas
auditáveis, não heurísticas escondidas.

## Tolerância a esquema

O esquema do `state.db` é do harness e muda. Em vez de fixar colunas, o leitor faz
`PRAGMA table_info` e monta a consulta com as colunas que existem:

```python
cols = set(table_columns(conn, "messages"))
select = ", ".join(f'"{c}"' for c in cols)
```

Consequência prática: uma coluna renomeada degrada um campo (`args_digest` fica `None`), mas não
quebra a coleta nem mente sobre o que foi lido. O campo `meta.row_id` sempre permite voltar à linha
original.

## Detecção

Oito regras, cada uma com um limiar em `DetectorConfig` e evidência rastreável:

| Arquivo | Regras | Ideia central |
| --- | --- | --- |
| `detectors/loops.py` | `repetition`, `no_progress`, `cycle`, `oscillation` | quatro assinaturas distintas de "não está progredindo" |
| `detectors/failures.py` | `budget_exhausted`, `persistence_locked`, `retry_storm`, `run_not_completed` | causas externas que encerram ou degradam a execução |

Decisões que merecem registro:

- **`cycle` varre todas as posições de início.** Um agente costuma fazer trabalho útil antes de entrar
  no laço; procurar o padrão apenas no começo da sequência perderia o caso mais comum.
- **`cycle` exige argumentos diferentes.** Se os argumentos são idênticos, o caso já é `repetition`;
  reportar os dois seria inflar a contagem do mesmo fenômeno.
- **`oscillation` exige uma única ferramenta.** Dois estados alternados de ferramentas diferentes não
  são "correção desfeita".
- **`repetition` tem janela temporal.** Repetições legítimas distantes no tempo (ex.: rodar a mesma
  suíte no começo e no fim do dia) não são loop.

## Camada de modelo

O Qwen3‑0.6B não substitui as regras; ele as **acompanha**. A avaliação roda os dois no mesmo
conjunto de janelas e publica a comparação, incluindo os erros do modelo com o motivo declarado por
ele. O modo de raciocínio fica desligado (`enable_thinking=False`) porque, em CPU, o custo por token
torna o benefício negativo — e um modelo de 0,6 B não sustenta cadeias longas de raciocínio úteis.

## Relatório

`report/build.py` é uma função pura: recebe três dicionários (sonda, detecção, benchmark) e devolve
Markdown. Nenhum número é digitado à mão. Se um limiar muda, o relatório muda — e o commit mostra a
diferença. Os gráficos são PNGs gerados por matplotlib em `reports/assets/`.

## Decisões de engenharia registradas

| Decisão | Alternativa descartada | Motivo |
| --- | --- | --- |
| `mode=ro` + `query_only` | abrir o banco normalmente e "só ler" | a garantia precisa ser imposta pelo SQLite, não pela disciplina do código |
| NDJSON append-only | banco próprio para os eventos | qualquer pessoa audita com `head`, `grep` e `jq` |
| regras antes do modelo | ir direto para o LLM | um achado precisa ser explicável e reprodutível para virar ação |
| fixture gerado pelo Hermes real | esquema inventado | validar contra a realidade é o único teste que importa |
| cenários com zona de correção | cenários só de falha | a pergunta útil não é "travou?", é "o que destravou?" |
| `hermes --version` com `HERMES_HOME` temporário | apontar para o home real | o CLI grava cache no home apontado; isolar mantém a promessa de não deixar rastro |
| leitura `immutable=1` opcional | usar `mode=ro` sempre | banco WAL em `mode=ro` faz o SQLite criar sidecars `-shm`/`-wal`; `immutable` evita, ao custo de exigir harness parado |
