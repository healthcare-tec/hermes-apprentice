#!/usr/bin/env python3
"""Cria um ``HERMES_HOME`` de laboratório usando uma instalação **real** do Hermes.

Por que isso existe: o coletor só é confiável se for validado contra o esquema
real do harness, e não contra um esquema que nós inventamos. Este script gera um
diretório isolado, roda comandos de leitura/escrita do próprio Hermes para
popular ``config.yaml`` e usa a API oficial (``hermes_state.SessionDB``) para
gravar uma sessão com mensagens e chamadas de ferramenta. O resultado é um
**fixture autêntico** usado por testes, exemplos e pela demonstração do coletor.

Nunca aponta para um ``HERMES_HOME`` de uso real: o caminho de saída é sempre um
diretório novo dentro do projeto (padrão: ``examples/hermes-home-fixture``).

Uso:
    python scripts/make_hermes_fixture.py \
        --hermes-bin ~/hermes-lab/venv/bin/hermes \
        --hermes-python ~/hermes-lab/venv/bin/python \
        --out examples/hermes-home-fixture
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

SEED_PROGRAM = r'''
import json, os, sys
from datetime import datetime, timezone
from hermes_state import SessionDB

db = SessionDB()
session_id = "sess_fixture_collector"
try:
    db.create_session(session_id=session_id, source="cli", model="nous/qwen3-0.6b", user_id="lab")
except Exception as exc:  # já existe
    print(f"[seed] sessão já existia: {exc}")

def add(**kwargs):
    db.append_message(session_id=session_id, **kwargs)

add(role="user", content="Revise o manual de integração e ajuste o passo 4.")
add(role="assistant", content="Vou ler o manual antes de propor o ajuste.",
    tool_calls=[{"id": "call_a1", "type": "function",
                 "function": {"name": "read_file", "arguments": json.dumps({"path": "docs/manual.md", "limit": 200})}}],
    finish_reason="tool_calls")
add(role="tool", tool_name="read_file", tool_call_id="call_a1",
    content="conteúdo do manual de integração (seção 1 a 6)")
add(role="assistant", content="Ajustei o passo 4.",
    tool_calls=[{"id": "call_a2", "type": "function",
                 "function": {"name": "edit_file", "arguments": json.dumps({"path": "docs/manual.md", "patch": "@@ passo 4"})}}],
    finish_reason="tool_calls")
add(role="tool", tool_name="edit_file", tool_call_id="call_a2", content="patch aplicado no passo 4")
add(role="user", content="Confirme o resultado.")
add(role="assistant", content="Confirmado: o passo 4 está correto.",
    tool_calls=[{"id": "call_a3", "type": "function",
                 "function": {"name": "read_file", "arguments": json.dumps({"path": "docs/manual.md", "limit": 200})}}],
    finish_reason="tool_calls")
add(role="tool", tool_name="read_file", tool_call_id="call_a3",
    content="Arquivo já lido nesta sessão; conteúdo idêntico ao anterior retornado por deduplicação.")
add(role="assistant", content="Vou reler o arquivo para confirmar o trecho.",
    tool_calls=[{"id": "call_a4", "type": "function",
                 "function": {"name": "read_file", "arguments": json.dumps({"path": "docs/manual.md", "limit": 200})}}],
    finish_reason="tool_calls")
add(role="tool", tool_name="read_file", tool_call_id="call_a4",
    content="Arquivo já lido nesta sessão; conteúdo idêntico ao anterior retornado por deduplicação.")
add(role="assistant", content="O arquivo continua idêntico; vou tentar novamente.")

db.end_session(session_id, end_reason="fixture_completed")
print("[seed] sessão de fixture criada:", session_id)
'''


def run(cmd: list[str], env: dict[str, str], *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"[!] comando falhou: {' '.join(cmd)}\n{result.stderr.strip()}", file=sys.stderr)
        raise SystemExit(1)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-bin", required=True, help="caminho do executável `hermes`")
    parser.add_argument("--hermes-python", default=None, help="interpretador do ambiente do Hermes")
    parser.add_argument("--out", default="examples/hermes-home-fixture")
    parser.add_argument("--force", action="store_true", help="sobrescreve o diretório de saída")
    args = parser.parse_args()

    out = Path(args.out).expanduser().resolve()
    stub = Path(__file__).resolve().parent.parent / "examples" / ".fixture-build"
    if stub.exists():
        shutil.rmtree(stub)
    stub.mkdir(parents=True, exist_ok=True)

    # Atenção: NÃO usar ``Path.resolve()`` aqui. Em um virtualenv, ``bin/python`` é
    # um symlink para o interpretador base; resolvê-lo descartaria o ambiente e o
    # pacote ``hermes_state`` deixaria de ser importável. Usamos caminho absoluto
    # sem seguir symlinks.
    def absolute(value: str) -> str:
        return os.path.abspath(os.path.expanduser(value))

    hermes_bin = absolute(args.hermes_bin)
    hermes_python = absolute(args.hermes_python) if args.hermes_python else None
    if not hermes_python:
        candidate = Path(hermes_bin).parent / "python"
        hermes_python = str(candidate) if candidate.exists() else sys.executable
    if not os.path.exists(hermes_python):
        print(f"[!] interpretador do Hermes não encontrado: {hermes_python}", file=sys.stderr)
        return 1

    env = dict(os.environ)
    env["HERMES_HOME"] = str(stub)

    print(f"[fixture] HERMES_HOME de laboratório: {stub}")
    for key, value in (
        ("model", "nous/qwen3-0.6b"),
        ("terminal.backend", "local"),
        ("tool_output.max_bytes", "20000"),
        ("agent.reasoning_effort", "low"),
    ):
        run([hermes_bin, "config", "set", key, value], env, check=False)
    run([hermes_bin, "--version"], env, check=False)

    print("[fixture] populando state.db pela API oficial do Hermes (SessionDB)")
    run([hermes_python, "-c", SEED_PROGRAM], env)

    state_db = stub / "state.db"
    if not state_db.exists():
        print("[!] state.db não foi criado; o Hermes instalado pode usar outro caminho", file=sys.stderr)
        return 1

    # O banco roda em modo WAL. Consolidamos o WAL no arquivo principal para que
    # o fixture seja autocontido: sem isso, parte das linhas viveria em
    # ``state.db-wal`` e quem copiasse só o ``state.db`` perderia dados.
    import sqlite3

    connection = sqlite3.connect(state_db)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        rows = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        connection.close()
    print(f"[fixture] integrity_check={result}, mensagens={rows}")

    if out.exists():
        if not args.force:
            print(f"[!] {out} já existe; use --force para sobrescrever", file=sys.stderr)
            return 1
        shutil.rmtree(out)
    out.mkdir(parents=True)

    # Copiamos apenas o que é seguro versionar: nada de `.env`, `auth.json`,
    # `.update_check` nem arquivos `-wal`/`-shm` (já consolidados acima).
    (out / "logs").mkdir(exist_ok=True)
    for relative in ("config.yaml", "SOUL.md", "state.db"):
        source = stub / relative
        if source.exists():
            shutil.copy2(source, out / relative)
    for log in (stub / "logs").glob("*.log"):
        shutil.copy2(log, out / "logs" / log.name)

    shutil.rmtree(stub, ignore_errors=True)
    print(f"[fixture] pronto em {out}")
    for item in sorted(out.rglob("*")):
        if item.is_file():
            print(f"  - {item.relative_to(out)} ({item.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
