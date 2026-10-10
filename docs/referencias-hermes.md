# Referências do harness Hermes

Registro das fontes externas usadas para desenhar o coletor, os detectores e os cenários. O objetivo
é que qualquer afirmação sobre o harness neste repositório possa ser conferida na origem.

## Repositório e distribuição

| Recurso | URL | O que foi usado |
| --- | --- | --- |
| Repositório oficial | https://github.com/NousResearch/hermes-agent | licença MIT, branch padrão `main`, descrição |
| Releases | https://github.com/NousResearch/hermes-agent/releases | última release observada: `v0.21.6` (2026‑10‑08) |
| Pacote PyPI | https://pypi.org/project/hermes-agent/ | versão `0.19.0`, `requires_python >=3.11,<3.14` |
| Issue citada no cenário 1 | https://github.com/NousResearch/hermes-agent/issues/15759 | *stub* de deduplicação de `read_file` que nunca dispara detecção de loop |
| Issue citada no cenário 5 | https://github.com/NousResearch/hermes-agent/issues?q=tool+loop | categoria de *tool loop* |

> **Divergência observada:** o PyPI serve `0.19.0` enquanto as releases do GitHub já estão em
> `v0.21.6`. A sonda registra isso automaticamente como aviso.

## Documentação oficial

| Página | URL | O que foi usado |
| --- | --- | --- |
| Configuração | https://hermes-agent.nousresearch.com/docs/user-guide/configuration | layout de `~/.hermes/`, `hermes config get/set`, `tool_output.max_bytes`, `agent.reasoning_effort` |
| Armazenamento de sessão | https://hermes-agent.nousresearch.com/docs/developer-guide/session-storage | `state.db` (SQLite WAL), tabela `sessions`/`messages`, `session_persistence_failed:locked`, `hermes_state_lockowners` |
| Hooks | https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks | catálogo de eventos (`pre_tool_call`, `post_tool_call`, `api_request_error`, …) e protocolo JSON por stdin/stdout |
| Integração programática | https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration | `POST /v1/runs`, `GET /v1/runs/{id}/events` (SSE), status terminal da execução (`completed`/`cancelled`/`failed`/`interrupted`), `turn_exit_reason` |
| Índice para LLMs | https://hermes-agent.nousresearch.com/docs/llms.txt | índice curado de toda a documentação |

## Trecho externo sobre stream de eventos

A issue [#30053](https://github.com/NousResearch/hermes-agent/issues/30053) pede um stream de eventos
estruturado para execuções não interativas. Foi encerrada como *not planned* com a justificativa de
que a capacidade já existe na **Runs API** (`POST /v1/runs` + `GET /v1/runs/{run_id}/events`), o que
confirma o SSE como contrato estável — e é a razão de ele ser a fonte preferida do nosso coletor.

## Fatos verificados na própria instalação

Medidos em ambiente isolado com Hermes Agent `v0.19.0 (2026.7.20)`, Python 3.12.3:

| Fato | Como foi verificado |
| --- | --- |
| `hermes_state.SessionDB` existe e é importável | `from hermes_state import SessionDB` dentro do venv do Hermes |
| `state.db` usa WAL e tem `sessions`, `messages`, `messages_fts*`, `schema_version`, `session_model_usage`, `state_meta` | `sqlite3_master` em leitura |
| `schema_version` da instalação observada | linha `[22]` |
| Colunas relevantes de `messages` | `id`, `session_id`, `role`, `content`, `tool_call_id`, `tool_calls`, `tool_name`, `timestamp`, `token_count`, `finish_reason`, `observed`, `active`, `compacted`, `api_content` |
| `SessionDB.append_message(session_id, role, content, tool_name, tool_calls, tool_call_id, …)` | assinatura inspecionada em execução |
| `hermes --version` grava `.update_check` no `HERMES_HOME` | observado no diretório de laboratório |
| Leitura SQLite de banco WAL em `mode=ro` cria `-shm`/`-wal`; `immutable=1` não cria | experimento controlado em `/tmp` (ver `docs/etica-e-limites.md`) |

Esses dois últimos itens são as descobertas que mais mudaram o desenho do projeto: a primeira levou
ao `HERMES_HOME` temporário na sonda, a segunda levou ao `--immutable`.