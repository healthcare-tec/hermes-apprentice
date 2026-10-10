"""Identificação da versão e da configuração atual do harness Hermes.

Regras de ouro deste módulo:

1. **Somente leitura.** Nenhum arquivo do Hermes é criado, alterado ou removido.
   O único processo externo executado é ``hermes --version`` (e, opcionalmente,
   ``hermes config get``), que é idempotente e não persiste estado. Use
   ``invoke_cli=False`` para desligar até isso.
2. **Degradação graciosa.** Em uma máquina sem Hermes instalado a sonda devolve
   ``available: false`` com o motivo, em vez de falhar.
3. **Redação.** Tudo que vem de ``.env``, ``auth.json`` ou de campos com
   ``KEY``/``TOKEN``/``SECRET`` no nome é borrado antes de sair do processo.

Resolução de ``HERMES_HOME`` (espelha ``get_hermes_home()`` do harness):
override explícito → variável de ambiente ``HERMES_HOME`` → padrão da plataforma
(``~/.hermes`` no Linux/macOS, ``%LOCALAPPDATA%/hermes`` no Windows).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import SCHEMA_VERSION, __version__
from ..redact import is_secret_path, redact_mapping, redact_text

PYPI_URL = "https://pypi.org/pypi/hermes-agent/json"
GITHUB_REPO_API = "https://api.github.com/repos/NousResearch/hermes-agent"

#: Arquivos/artefatos conhecidos do layout de ``~/.hermes`` (documentação oficial).
KNOWN_HOME_ENTRIES = [
    "config.yaml",
    ".env",
    "auth.json",
    "SOUL.md",
    "memories",
    "skills",
    "cron",
    "sessions",
    "logs",
    "state.db",
    "plugins",
    "hooks",
    "agent-hooks",
    "profiles",
    "cache",
]


def resolve_hermes_home(explicit: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Resolve o diretório ``HERMES_HOME`` e informa qual regra foi usada."""
    env = env if env is not None else os.environ
    if explicit:
        return {"path": str(Path(explicit).expanduser()), "source": "explicit"}
    if env.get("HERMES_HOME"):
        return {"path": str(Path(env["HERMES_HOME"]).expanduser()), "source": "HERMES_HOME"}
    if sys.platform.startswith("win"):
        base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return {"path": str(Path(base) / "hermes"), "source": "platform_default"}
    return {"path": str(Path.home() / ".hermes"), "source": "platform_default"}


def _run(cmd: List[str], *, timeout: int = 25, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Executa um comando de leitura e devolve stdout/stderr/returncode sem levantar exceção."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env, check=False
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except FileNotFoundError:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": "executável não encontrado"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": f"timeout após {timeout}s"}
    except OSError as exc:  # pragma: no cover - depende do SO
        return {"ok": False, "returncode": None, "stdout": "", "stderr": str(exc)}


def _http_json(url: str, *, timeout: int = 20) -> Optional[Dict[str, Any]]:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": f"hermes-apprentice/{__version__}"})
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL fixa
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _parse_yaml_minimal(text: str) -> Dict[str, Any]:
    """Leitor YAML tolerante (subconjunto) usado quando PyYAML não está disponível.

    Cobre o formato plano/aninhado por indentação que o ``config.yaml`` do
    Hermes usa. Não é um parser YAML completo — é a rede de segurança para
    rodar a sonda em ambientes mínimos.
    """
    root: Dict[str, Any] = {}
    stack: List[tuple[int, Dict[str, Any]]] = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().strip("'\"")
        value = value.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1] if stack else root
        if value == "":
            child: Dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = value.strip("'\"")
    return root


def read_config(config_path: Path) -> Dict[str, Any]:
    """Lê ``config.yaml`` sem escrever nada e devolve um resumo redigido."""
    info: Dict[str, Any] = {"path": str(config_path), "exists": config_path.exists()}
    if not config_path.exists():
        info["reason"] = "config.yaml ausente"
        return info
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        info["reason"] = f"falha de leitura: {exc}"
        return info

    try:  # PyYAML é opcional
        import yaml  # type: ignore

        parsed: Dict[str, Any] = yaml.safe_load(text) or {}
        info["parser"] = "pyyaml"
    except Exception:  # noqa: BLE001 - fallback deliberado
        parsed = _parse_yaml_minimal(text)
        info["parser"] = "minimal"

    if not isinstance(parsed, dict):
        parsed = {}
    info["size_bytes"] = len(text.encode("utf-8"))
    info["sha256_prefix"] = __import__("hashlib").sha256(text.encode("utf-8")).hexdigest()[:12]
    info["top_level_keys"] = sorted(parsed.keys())
    info["values"] = redact_mapping(parsed)
    # Campos de interesse operacional, sem segredos.
    info["highlights"] = {
        "model": parsed.get("model"),
        "provider": parsed.get("provider") or (parsed.get("model_config") or {}).get("provider")
        if isinstance(parsed.get("model_config"), dict)
        else parsed.get("provider"),
        "agent": {
            key: value
            for key, value in (parsed.get("agent") or {}).items()
            if key in {"reasoning_effort", "text_verbosity", "disabled_toolsets", "max_iterations"}
        }
        if isinstance(parsed.get("agent"), dict)
        else None,
        "tool_output": parsed.get("tool_output"),
        "terminal_backend": (parsed.get("terminal") or {}).get("backend")
        if isinstance(parsed.get("terminal"), dict)
        else None,
    }
    return info


def inspect_state_db(db_path: Path, *, immutable: bool = True) -> Dict[str, Any]:
    """Inspeciona ``state.db`` sem escrever nada.

    Usa ``immutable=1`` por padrão porque a sonda só precisa de contagens e de
    nomes de tabela: assim **nenhum** arquivo sidecar (``-shm``/``-wal``) é criado
    no diretório do usuário. Uma visão levemente desatualizada é um preço
    aceitável por não deixar rastro.
    """
    info: Dict[str, Any] = {"path": str(db_path), "exists": db_path.exists()}
    if not db_path.exists():
        info["reason"] = "state.db ausente (nenhuma sessão persistida ainda)"
        return info
    info["size_bytes"] = db_path.stat().st_size
    info["wal_present"] = db_path.with_name(db_path.name + "-wal").exists()
    try:
        uri = f"file:{db_path.as_posix()}?mode=ro"
        if immutable:
            uri += "&immutable=1"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            if not immutable:
                conn.execute("PRAGMA query_only = ON")
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
                )
            ]
            info["tables"] = tables
            info["journal_mode"] = (conn.execute("PRAGMA journal_mode").fetchone() or ["?"])[0]
            counts: Dict[str, int] = {}
            for table in ("sessions", "messages", "session_model_usage", "schema_version"):
                if table in tables:
                    try:
                        counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
                    except sqlite3.DatabaseError:
                        continue
            info["counts"] = counts
            if "schema_version" in tables:
                try:
                    row = conn.execute("SELECT * FROM schema_version LIMIT 1").fetchone()
                    info["schema_version_row"] = list(row) if row else None
                except sqlite3.DatabaseError:
                    pass
            # Ressalva: abrir em ro pode falhar se o lock WAL estiver ativo.
        finally:
            conn.close()
        info["read_only"] = True
    except sqlite3.DatabaseError as exc:
        info["reason"] = f"não foi possível abrir em modo somente leitura: {exc}"
        info["read_only"] = True
    info["immutable"] = immutable
    info["sidecars"] = sorted(
        path.name for path in (db_path.with_name(db_path.name + s) for s in ("-shm", "-wal")) if path.exists()
    )
    return info


def inspect_logs(home: Path, *, max_files: int = 8, excerpt_chars: int = 400) -> Dict[str, Any]:
    """Lista logs e guarda apenas o final redigido de cada um."""
    logs_dir = home / "logs"
    result: Dict[str, Any] = {"dir": str(logs_dir), "exists": logs_dir.exists(), "files": []}
    if not logs_dir.exists():
        return result
    files = sorted((p for p in logs_dir.rglob("*") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[:max_files]:
        try:
            tail = path.read_text(encoding="utf-8", errors="replace")[-excerpt_chars:]
        except OSError:
            continue
        result["files"].append(
            {
                "name": str(path.relative_to(home)),
                "size_bytes": path.stat().st_size,
                "tail": redact_text(tail, max_len=excerpt_chars),
            }
        )
    return result


def list_profiles(home: Path) -> List[Dict[str, Any]]:
    """Lista perfis nomeados (``<home>/profiles/<nome>``), que são isolados."""
    profiles_dir = home / "profiles"
    if not profiles_dir.is_dir():
        return []
    out = []
    for child in sorted(profiles_dir.iterdir()):
        if child.is_dir():
            out.append(
                {
                    "name": child.name,
                    "has_config": (child / "config.yaml").exists(),
                    "has_state_db": (child / "state.db").exists(),
                }
            )
    return out


def probe_environment(
    hermes_home: Optional[str] = None,
    *,
    hermes_bin: Optional[str] = None,
    invoke_cli: bool = True,
    network: bool = True,
) -> Dict[str, Any]:
    """Executa a sondagem completa e devolve um retrato JSON-serializável."""
    home_info = resolve_hermes_home(hermes_home)
    home = Path(home_info["path"])
    notes: List[str] = []

    available = home.exists()
    entries = {}
    if available:
        entries = {
            name: (home / name).is_dir() and "dir" or "file"
            for name in KNOWN_HOME_ENTRIES
            if (home / name).exists()
        }
    else:
        notes.append(f"HERMES_HOME resolvido para {home}, mas o diretório não existe.")

    # --- CLI ---------------------------------------------------------------
    binary = hermes_bin or shutil.which("hermes")
    cli: Dict[str, Any] = {"binary": binary, "available": bool(binary), "invoked": False}
    if binary and invoke_cli:
        # O CLI do Hermes grava arquivos de cache no HERMES_HOME sempre que roda
        # (`.update_check`, e às vezes `-wal`/`-shm` ao abrir o banco). Executá-lo
        # contra a instalação real quebraria a promessa de somente observação.
        # Como a saída de `--version` vem dos metadados do pacote — não da
        # configuração —, apontamos HERMES_HOME para um diretório temporário: o
        # versionamento é obtido sem tocar em nada do usuário.
        with tempfile.TemporaryDirectory(prefix="hermes-apprentice-probe-") as sandbox_home:
            env = dict(os.environ)
            env["HERMES_HOME"] = sandbox_home
            version = _run([binary, "--version"], env=env)
        cli["invoked"] = True
        cli["hermes_home_used"] = "temporary_directory"
        cli["version_raw"] = redact_text(version["stdout"] or version["stderr"], max_len=300)
        cli["version_ok"] = version["ok"]
        if not version["ok"]:
            notes.append("`hermes --version` falhou; a versão foi inferida pelos metadados do pacote.")
        notes.append(
            "A chamada `hermes --version` foi executada com HERMES_HOME temporário: o CLI do harness "
            "grava cache no HERMES_HOME, e apontá-lo para o diretório real violaria a garantia de "
            "somente observação."
        )
    elif binary:
        notes.append("Chamada ao CLI desativada (invoke_cli=False); versão não coletada em execução.")

    return _finalize(home_info, home, available, entries, cli, notes, network)


def _finalize(
    home_info: Dict[str, Any],
    home: Path,
    available: bool,
    entries: Dict[str, str],
    cli: Dict[str, Any],
    notes: List[str],
    network: bool,
) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "machine": platform.machine(),
            "cwd": os.getcwd(),
        },
        "hermes_home": {**home_info, "exists": available, "entries": entries},
        "installation": {
            "cli": cli,
            "pip_metadata": _pip_metadata(cli.get("binary")),
        },
        "profiles": list_profiles(home),
        "config": read_config(home / "config.yaml"),
        "state_db": inspect_state_db(home / "state.db"),
        "logs": inspect_logs(home),
        "secrets_present": {
            name: (home / name).exists() for name in (".env", "auth.json") if (home / name).exists()
        },
        "upstream": {},
        "notes": list(notes),
    }

    if network:
        pypi = _http_json(PYPI_URL)
        if pypi:
            report["upstream"]["pypi"] = {
                "latest": (pypi.get("info") or {}).get("version"),
                "requires_python": (pypi.get("info") or {}).get("requires_python"),
                "releases": len(pypi.get("releases") or {}),
            }
        gh = _http_json(GITHUB_REPO_API)
        if gh:
            report["upstream"]["github"] = {
                "stars": gh.get("stargazers_count"),
                "license": (gh.get("license") or {}).get("spdx_id"),
                "default_branch": gh.get("default_branch"),
                "pushed_at": gh.get("pushed_at"),
            }
        rel = _http_json(GITHUB_REPO_API + "/releases/latest")
        if rel:
            report["upstream"]["github_latest_release"] = {
                "tag": rel.get("tag_name"),
                "published_at": rel.get("published_at"),
            }
        if not report["upstream"]:
            notes.append("Sem acesso de rede: metadados de upstream não coletados.")

    # Divergência PyPI x release do GitHub é um achado relevante de diagnóstico.
    pip_version = (report["installation"]["pip_metadata"] or {}).get("version")
    gh_tag = (report["upstream"].get("github_latest_release") or {}).get("tag")
    if pip_version and gh_tag and gh_tag.lstrip("v") != pip_version:
        report["notes"].append(
            f"Divergência de versões: pacote PyPI {pip_version} x última release do GitHub {gh_tag}."
        )
    return report


def _pip_metadata(binary: Optional[str] = None) -> Dict[str, Any]:
    """Lê os metadados do pacote sem importá-lo (importar o Hermes é invasivo).

    A primeira tentativa é o interpretador atual. A segunda é o *ambiente* do
    binário ``hermes`` — o caso comum quando o harness vive em um virtualenv
    próprio e a sonda roda em outro interpretador. Reportar ``None`` aqui faria a
    divergência de versões do relatório parecer inexistente.
    """
    import importlib.metadata as md

    try:
        dist = md.distribution("hermes-agent")
        return {
            "version": dist.version,
            "requires_python": (dist.metadata or {}).get("Requires-Python"),
            "location": str(Path(dist.locate_file("")).resolve()),
            "source": "current_interpreter",
        }
    except md.PackageNotFoundError:
        pass

    if binary:
        resolved = Path(binary).resolve()
        venv_root = resolved.parent.parent
        patterns = [
            "lib/python*/site-packages/hermes_agent-*.dist-info/METADATA",
            "lib64/python*/site-packages/hermes_agent-*.dist-info/METADATA",
            "Lib/site-packages/hermes_agent-*.dist-info/METADATA",
        ]
        for pattern in patterns:
            for candidate in venv_root.glob(pattern):
                for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("Version:"):
                        return {
                            "version": line.split(":", 1)[1].strip(),
                            "location": str(venv_root),
                            "source": "hermes_binary_environment",
                        }
    return {"version": None, "reason": "pacote hermes-agent não está instalado neste interpretador"}
