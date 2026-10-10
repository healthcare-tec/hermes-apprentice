"""Redação de segredos.

O coletor é *somente observação*, mas observar ainda significa **ler** conteúdo
sensível: comandos de terminal, argumentos de ferramentas, chaves de API dentro
de ``.env`` e mensagens de erro de provedores. Todo texto que sai do processo
passa por ``redact()`` antes de ser gravado em NDJSON ou em relatório.

A redação é deliberadamente conservadora e determinística: prefere borrar demais
a vazar. Ela **não** é um mecanismo de segurança para produção — é uma barreira
de higiene de dados para pesquisa e compartilhamento.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

#: Marcadores de chave/segredo por prefixo conhecido.
_PREFIX_PATTERNS: List[re.Pattern[str]] = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"\bsk-or-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{10,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{6,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{10,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{10,}"),
    re.compile(r"\bnvapi-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
]

#: Pares ``CHAVE=valor`` em texto tipo .env / variável de ambiente.
_ASSIGNMENT_PATTERN = re.compile(
    r"(?P<key>\b[A-Z][A-Z0-9_]{2,}(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|COOKIE|SESSION)[A-Z0-9_]*)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<value>[^\s,;'\"]+)",
)

_BEARER_PATTERN = re.compile(r"(?i)\b(bearer|token|api[_-]?key)\s*[:=]?\s*([A-Za-z0-9_\-\.]{12,})")

#: Caminhos de segredo do Hermes que nunca devem aparecer crus.
_HERMES_SECRET_FILES = (".env", "auth.json", ".secrets")

REDACTED = "[REDACTED]"


def redact_text(text: str, *, max_len: int = 500) -> str:
    """Aplica todas as regras de redação e trunca o resultado."""
    if text is None:
        return ""
    value = str(text)
    for pattern in _PREFIX_PATTERNS:
        value = pattern.sub(REDACTED, value)
    value = _ASSIGNMENT_PATTERN.sub(
        lambda m: f"{m.group('key')}{m.group('sep')}{REDACTED}", value
    )
    value = _BEARER_PATTERN.sub(lambda m: f"{m.group(1)} {REDACTED}", value)
    if len(value) > max_len:
        value = value[:max_len] + "…[truncado]"
    return value


def is_secret_path(path: str) -> bool:
    """Indica se um caminho do Hermes aponta para um arquivo de segredos."""
    tail = str(path).replace("\\", "/").rstrip("/").split("/")[-1]
    return tail in _HERMES_SECRET_FILES


def redact_mapping(data: Dict[str, Any], *, max_len: int = 500) -> Dict[str, Any]:
    """Redige recursivamente um dicionário (usado para config e meta)."""
    out: Dict[str, Any] = {}
    for key, value in data.items():
        if is_secret_path(key) or any(
            marker in str(key).upper()
            for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
        ):
            out[key] = REDACTED
        elif isinstance(value, dict):
            out[key] = redact_mapping(value, max_len=max_len)
        elif isinstance(value, list):
            out[key] = [
                redact_mapping(v, max_len=max_len) if isinstance(v, dict) else redact_text(str(v), max_len=max_len)
                for v in value
            ]
        elif isinstance(value, str):
            out[key] = redact_text(value, max_len=max_len)
        else:
            out[key] = value
    return out