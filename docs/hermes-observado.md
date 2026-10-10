# O Hermes observado

Registro de qual harness foi sondado, como, e o que a sonda encontrou. Serve para dar contexto aos
números do relatório e para permitir que outra pessoa reproduza a mesma observação.

## Como a sonda foi feita

O Hermes foi instalado **isoladamente**, sem tocar em nenhuma instalação de uso real:

```bash
python3 -m venv ~/hermes-lab/venv
~/hermes-lab/venv/bin/pip install hermes-agent
mkdir -p ~/hermes-lab/home
HERMES_HOME=~/hermes-lab/home ~/hermes-lab/venv/bin/hermes --version
```

O `HERMES_HOME` de laboratório foi então populado pelo próprio CLI (`hermes config set …`), o que
produziu um `config.yaml`, um `SOUL.md`, `.env` e `logs/` autênticos. Uma sessão de fixture foi
gravada pela API oficial (`hermes_state.SessionDB`), gerando um `state.db` com o esquema real — o
mesmo usado em [`examples/hermes-home-fixture/`](../examples/hermes-home-fixture).

## Resultado da sonda

| Item | Valor observado |
| --- | --- |
| Versão do CLI | `Hermes Agent v0.19.0 (2026.7.20)` |
| Método de instalação | `pip` (PyPI) |
| Python do ambiente | 3.12.3 |
| OpenAI SDK | 2.24.0 |
| Pacote PyPI | `hermes-agent` 0.19.0 (`requires_python >=3.11,<3.14`) |
| Última release no GitHub | `v0.21.6` (2026-10-08) |
| Licença / stars upstream | MIT / ~252 mil |
| `HERMES_HOME` | resolvido por override explícito → variável de ambiente → padrão da plataforma |

**Achado de diagnóstico:** a distribuição no PyPI (0.19.0) fica atrás das releases do GitHub
(v0.21.6). Quem instala por `pip` e confere a versão em `hermes --version` pode acreditar estar em
uma versão muito mais antiga do que a documentação descreve. A sonda registra essa divergência
automaticamente (`notes` em `artifacts/hermes-environment.json`).

## Ordem de resolução de `HERMES_HOME`

A sonda espelha `get_hermes_home()` do harness:

1. parâmetro explícito (`--hermes-home`);
2. variável de ambiente `HERMES_HOME`;
3. padrão da plataforma (`~/.hermes` no Linux/macOS, `%LOCALAPPDATA%/hermes` no Windows).

Perfis nomeados vivem em `<home>/profiles/<nome>/` e são isolados: cada perfil tem seu próprio
`state.db`, configuração e logs. A sonda lista os perfis e sinaliza quais têm banco e configuração.

## Superfícies de observação disponíveis (documentação oficial)

| Superfície | Onde | Usada por nós |
| --- | --- | --- |
| `state.db` (SQLite, WAL) | `$HERMES_HOME/state.db` | `collector/state_db.py` |
| Logs | `$HERMES_HOME/logs/*.log` | `collector/logs.py` |
| Runs API (SSE) | `GET /v1/runs/{id}/events` | `collector/runs_api.py` |
| Hooks de shell / plugin | `hooks:` em `config.yaml` | `collector/spool.py` (opt-in) |
| TUI gateway (JSON-RPC) | `tui_gateway/server.py` | não usado (exige conexão bidirecional) |
| ACP | `hermes acp` | não usado (protocolo de IDE) |

## Vocabulário do harness preservado no nosso código

| Harness | Nosso esquema |
| --- | --- |
| `run.started` / `run.completed` / `run.failed` / `run.cancelled` | `run_start` / `run_end` (+ `status`) |
| `tool.started` / `tool.completed` | `tool_call_start` / `tool_call_end` |
| `assistant.delta` / `assistant.completed` | `assistant_delta` / `assistant_message` |
| `pre_tool_call` / `post_tool_call` (hooks) | `tool_call_start` / `tool_call_end` |
| `api_request_error` (hook) | `api_error` |
| `session_persistence_failed:locked` (log) | `persistence_failure` |
| `max_iterations_reached(N/N)` (`turn_exit_reason`) | `budget_exhausted` |

## Reproduzindo a observação

```bash
# sonda
python -m hermes_apprentice probe --hermes-home ~/hermes-lab/home \
    --hermes-bin ~/hermes-lab/venv/bin/hermes --out artifacts/hermes-environment.json

# fixture autêntico (gera config + state.db pela API oficial)
python scripts/make_hermes_fixture.py \
    --hermes-bin ~/hermes-lab/venv/bin/hermes \
    --hermes-python ~/hermes-lab/venv/bin/python \
    --out examples/hermes-home-fixture --force
```

> Nota de implementação descoberta na prática: em um virtualenv, `bin/python` é um symlink para o
> interpretador base. Resolver o symlink (`Path.resolve()`) descarta o ambiente e torna
> `hermes_state` não importável. O script usa caminho absoluto sem seguir symlinks — o comentário no
> código registra isso para quem repetir o procedimento.