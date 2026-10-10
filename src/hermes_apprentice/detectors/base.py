"""Estruturas comuns aos detectores determinísticos.

Os detectores são **explicáveis por construção**: cada achado aponta os índices
dos eventos que o sustentam e o limiar que foi violado. Sem isso, um relatório de
loops vira opinião.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..schema import Event

SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3}


@dataclass
class DetectorConfig:
    """Limiares ajustáveis. Os padrões foram escolhidos para terem baixo falso-positivo
    em sessões normais do harness."""

    #: Quantas vezes a mesma assinatura (ferramenta+argumentos) pode repetir antes de virar loop.
    repetition_min: int = 3
    #: Quantas repetições do mesmo resultado antes de considerar "sem progresso real".
    no_progress_min: int = 3
    #: Período máximo do ciclo de ferramentas (A→B→A→B é período 2).
    cycle_max_period: int = 4
    #: Quantas voltas completas do ciclo antes de acusar.
    cycle_min_repeats: int = 3
    #: Quantos erros de API em sequência caracterizam tempestade de retries.
    retry_storm_min: int = 3
    #: Janela (em segundos) para considerar eventos "na mesma rajada". 0 desativa o corte temporal.
    burst_window_s: float = 120.0
    #: Um mesmo resultado repetido após este número de chamadas indica *stub* de deduplicação.
    dedup_stub_min: int = 2


@dataclass
class Finding:
    """Um achado com evidência rastreável."""

    rule: str
    category: str  # "loop" | "failure"
    severity: str
    message: str
    session_id: Optional[str] = None
    start_ts: Optional[str] = None
    end_ts: Optional[str] = None
    evidence: List[int] = field(default_factory=list)
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def signature(self) -> str:
        """Identidade estável do achado (para deduplicar entre fontes)."""
        return f"{self.rule}|{self.session_id}|{self.detail.get('tool') or ''}|{self.detail.get('signature') or ''}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.rule,
            "category": self.category,
            "severity": self.severity,
            "message": self.message,
            "session_id": self.session_id,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "evidence": self.evidence,
            "detail": self.detail,
        }


def group_by_session(events: Sequence[Event]) -> Dict[str, List[int]]:
    """Índices por sessão; eventos sem sessão vão para o grupo ``<sem-sessao>``."""
    groups: Dict[str, List[int]] = {}
    for index, event in enumerate(events):
        groups.setdefault(event.session_id or "<sem-sessao>", []).append(index)
    return groups


def seconds_between(a: Optional[str], b: Optional[str]) -> Optional[float]:
    from datetime import datetime

    def parse(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    first, second = parse(a), parse(b)
    if first is None or second is None:
        return None
    return abs((second - first).total_seconds())


def worst_severity(findings: Sequence[Finding]) -> Optional[str]:
    if not findings:
        return None
    return max((f.severity for f in findings), key=lambda s: SEVERITY_ORDER.get(s, 0))