"""Leitor **somente leitura** do ``state.db`` do Hermes.

Garantias de segurança deste módulo:

* A conexão é aberta com URI ``file:...?mode=ro`` **e** ``PRAGMA query_only=ON``.
  Tentar um ``INSERT``/``UPDATE``/``DELETE`` levanta ``sqlite3.OperationalError``.
* Nunca executamos ``VACUUM``, checkpoint de WAL, migração de schema ou qualquer
  escrita. Se o banco estiver com lock de escrita, apenas reportamos.
* O leitor é tolerante a schema: usamos ``PRAGMA table_info`` para descobrir as
  colunas existentes. Colunas novas ou renomeadas pelo harness não quebram a
  coleta — o evento correspondente sai com os campos disponíveis em ``meta``.

Como o banco é compartilhado entre gateway, CLI e subagentes (WAL, um escritor
por vez), a leitura é feita com ``timeout`` curto e reprocessamento incremental
por ``rowid``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from ..redact import redact_text
from ..schema import Event, digest


def connect_readonly(db_path: str | Path, *, timeout: float = 5.0) -> sqlite3.Connection:
    """Abre ``state.db`` garantidamente em modo somente leitura."""
    path = Path(db_path)
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]  # noqa: S608
    except sqlite3.DatabaseError:
        return []


def list_tables(conn: sqlite3.Connection) -> List[str]:
    try:
        return [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
            )
        ]
    except sqlite3.DatabaseError:
        return []


def _pick(row: Dict[str, Any], *candidates: str, default: Any = None) -> Any:
    for name in candidates:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return default


def _json_load(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip().startswith(("{", "[")):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return None


def _to_iso(ts: Any) -> str:
    """Converte epoch (int/float) ou string em ISO-8601 UTC."""
    from datetime import datetime, timezone

    if ts in (None, ""):
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if isinstance(ts, (int, float)):
        seconds = ts / 1000.0 if ts > 1e11 else float(ts)  # heurística ms x s
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            )
        except (ValueError, OSError, OverflowError):
            pass
    return str(ts)


def iter_session_events(conn: sqlite3.Connection, *, limit: int = 2000) -> Iterator[Event]:
    """Emite ``session_start``/``session_end`` a partir da tabela ``sessions``."""
    tables = list_tables(conn)
    if "sessions" not in tables:
        return
    cols = set(table_columns(conn, "sessions"))
    select = [c for c in ("id", "source", "model", "started_at", "ended_at", "end_reason",
                          "message_count", "tool_call_count", "parent_session_id", "title") if c in cols]
    if not select:
        return
    query = f"SELECT {', '.join(select)} FROM sessions ORDER BY started_at DESC LIMIT ?"  # noqa: S608
    for row in conn.execute(query, (limit,)):
        record = dict(row)
        session_id = record.get("id")
        yield Event(
            type="session_start",
            ts=_to_iso(record.get("started_at")),
            source="state_db",
            session_id=session_id,
            model=record.get("model"),
            meta={
                "source_platform": record.get("source"),
                "parent_session_id": record.get("parent_session_id"),
                "title": redact_text(str(record.get("title") or ""), max_len=120),
                "message_count": record.get("message_count"),
                "tool_call_count": record.get("tool_call_count"),
            },
        )
        if record.get("ended_at"):
            yield Event(
                type="session_end",
                ts=_to_iso(record.get("ended_at")),
                source="state_db",
                session_id=session_id,
                status="ok",
                meta={"end_reason": record.get("end_reason")},
            )


def iter_message_events(
    conn: sqlite3.Connection,
    *,
    since_row_id: int = 0,
    limit: int = 5000,
) -> Iterator[Event]:
    """Converte linhas de ``messages`` em eventos normalizados.

    Mapeamento (tolerante a ausência de colunas):

    * ``role=user``        → turn_start
    * ``role=assistant``   → assistant_message  (+ tool_call_start por tool_call)
    * ``role=tool``        → tool_result
    * qualquer ``error*``  → error
    """
    tables = list_tables(conn)
    if "messages" not in tables:
        return
    cols = table_columns(conn, "messages")
    if not cols:
        return
    colset = set(cols)
    order_col = "rowid" if "id" not in colset else "id"
    select = ", ".join(f'"{c}"' for c in cols)
    query = (
        f'SELECT rowid AS _rowid, {select} FROM messages '  # noqa: S608
        f"WHERE rowid > ? ORDER BY rowid ASC LIMIT ?"
    )
    for row in conn.execute(query, (since_row_id, limit)):
        record = dict(row)
        record.pop("_rowid", None)
        row_id = row["_rowid"]
        role = str(_pick(record, "role", "type", "kind", default="unknown")).lower()
        ts = _to_iso(_pick(record, "created_at", "timestamp", "ts", "started_at"))
        session_id = _pick(record, "session_id", "session")
        turn_id = _pick(record, "turn_id", "turn")
        content = _pick(record, "content", "text", "message", default="")
        tool_name = _pick(record, "tool_name", "name", "tool")
        tool_calls = _json_load(_pick(record, "tool_calls", "tool_call", "tool_calls_json"))
        error_text = _pick(record, "error", "error_message", "exception")

        base = dict(
            source="state_db",
            session_id=session_id,
            turn_id=str(turn_id) if turn_id else None,
            seq=row_id,
            meta={"row_id": row_id, "role": role},
        )

        if error_text:
            yield Event(
                type="error",
                ts=ts,
                status="error",
                error_message=redact_text(str(error_text), max_len=300),
                **base,
            )

        if role == "user":
            yield Event(type="turn_start", ts=ts, text_preview=redact_text(str(content), max_len=300), **base)
        elif role in {"assistant", "ai", "model"}:
            yield Event(
                type="assistant_message",
                ts=ts,
                text_preview=redact_text(str(content), max_len=400),
                **base,
            )
            for call in (tool_calls or []):
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                name = call.get("name") or (fn.get("name") if isinstance(fn, dict) else None)
                raw_args = call.get("arguments") or (fn.get("arguments") if isinstance(fn, dict) else None)
                args_obj = _json_load(raw_args) or (raw_args if isinstance(raw_args, dict) else {})
                yield Event(
                    type="tool_call_start",
                    ts=ts,
                    tool=str(name) if name else "unknown",
                    tool_call_id=call.get("id") or call.get("tool_call_id"),
                    args_digest=digest(name, args_obj),
                    args_preview=redact_text(json.dumps(args_obj, ensure_ascii=False), max_len=240),
                    **base,
                )
        elif role in {"tool", "tool_result", "function"}:
            yield Event(
                type="tool_result",
                ts=ts,
                tool=str(tool_name) if tool_name else "unknown",
                tool_call_id=_pick(record, "tool_call_id", "call_id"),
                result_digest=digest(content) if content else None,
                text_preview=redact_text(str(content), max_len=300),
                status=_pick(record, "status", default="ok"),
                **base,
            )
        else:
            yield Event(type="unknown", ts=ts, text_preview=redact_text(str(content), max_len=200), **base)


def iter_events(
    db_path: str | Path,
    *,
    since_row_id: int = 0,
    limit: int = 5000,
) -> Iterator[Event]:
    """Ponto de entrada: emite todos os eventos disponíveis, ordenados por ``rowid``."""
    conn = connect_readonly(db_path)
    try:
        yield from iter_session_events(conn, limit=min(limit, 2000))
        yield from iter_message_events(conn, since_row_id=since_row_id, limit=limit)
    finally:
        conn.close()


def max_row_id(db_path: str | Path) -> int:
    """Maior ``rowid`` já processado — usado para coleta incremental."""
    conn = connect_readonly(db_path)
    try:
        if "messages" not in list_tables(conn):
            return 0
        row = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM messages").fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def assert_readonly(db_path: str | Path) -> Dict[str, Any]:
    """Autoteste: prova que a conexão recusa escrita. Usado nos testes e no relatório."""
    conn = connect_readonly(db_path)
    result: Dict[str, Any] = {"path": str(db_path)}
    try:
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS _apprentice_probe (x INTEGER)")
            result["write_blocked"] = False
            result["error"] = "escrita aceita — INVESTIGAR"
        except sqlite3.OperationalError as exc:
            result["write_blocked"] = True
            result["error"] = str(exc)
    finally:
        conn.close()
    return result