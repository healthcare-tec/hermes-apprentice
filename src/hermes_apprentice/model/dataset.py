"""Construção do conjunto de janelas rotuladas a partir dos cenários.

Cada cenário declara ``zonas de rótulo``: faixas de índices de evento com o
veredito esperado (``normal`` / ``loop`` / ``failure``). O construtor desliza uma
janela de tamanho fixo dentro de cada zona e herda o rótulo da zona majoritária.
As janelas ficam **disjuntas entre zonas de rótulos diferentes**, o que evita
contaminação entre treino de avaliação e alvo.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..schema import Event


@dataclass
class Window:
    """Uma janela de eventos com rótulo de referência."""

    scenario_id: str
    index: int
    start: int
    end: int
    label: str
    events: List[Event] = field(default_factory=list)
    loop_type: str = "none"
    phase: str = ""

    @property
    def size(self) -> int:
        return len(self.events)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "label": self.label,
            "loop_type": self.loop_type,
            "phase": self.phase,
            "size": self.size,
        }


def load_scenario(path: str | Path) -> Dict[str, Any]:
    """Carrega ``scenario.json`` e os eventos associados."""
    directory = Path(path)
    meta = json.loads((directory / "scenario.json").read_text(encoding="utf-8"))
    events: List[Event] = []
    events_file = directory / meta.get("events_file", "events.ndjson")
    with events_file.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            events.append(Event.from_dict(json.loads(line)))
    meta["_events"] = events
    meta["_dir"] = str(directory)
    meta["id"] = meta.get("id") or directory.name
    return meta


def build_windows(
    scenario: Dict[str, Any],
    *,
    window_size: int = 12,
    stride: int = 6,
    max_windows_per_zone: int = 6,
) -> List[Window]:
    """Gera janelas rotuladas a partir das zonas declaradas no cenário."""
    events: List[Event] = scenario["_events"]
    windows: List[Window] = []
    counter = 0
    for zone in scenario.get("label_zones", []):
        first = int(zone.get("from_event", 0))
        last = int(zone.get("to_event", len(events) - 1))
        label = zone.get("label", "normal")
        loop_type = zone.get("loop_type", "none")
        phase = zone.get("phase", "")
        span = last - first + 1
        if span < window_size:
            continue
        produced = 0
        for start in range(first, last - window_size + 2, stride):
            if produced >= max_windows_per_zone:
                break
            chunk = events[start : start + window_size]
            if len(chunk) < window_size:
                break
            windows.append(
                Window(
                    scenario_id=scenario["id"],
                    index=counter,
                    start=start,
                    end=start + len(chunk) - 1,
                    label=label,
                    loop_type=loop_type,
                    phase=phase,
                    events=chunk,
                )
            )
            produced += 1
            counter += 1
    return windows


def build_dataset(
    scenarios_dir: str | Path,
    *,
    window_size: int = 12,
    stride: int = 6,
    max_windows_per_zone: int = 6,
    include: Optional[Sequence[str]] = None,
) -> List[Window]:
    """Constrói o conjunto completo a partir do diretório de cenários."""
    directory = Path(scenarios_dir)
    windows: List[Window] = []
    for scenario_file in sorted(directory.glob("*/scenario.json")):
        scenario = load_scenario(scenario_file.parent)
        if include and scenario["id"] not in include:
            continue
        if not scenario.get("label_zones"):
            continue
        windows.extend(
            build_windows(
                scenario,
                window_size=window_size,
                stride=stride,
                max_windows_per_zone=max_windows_per_zone,
            )
        )
    return windows


def dataset_summary(windows: Sequence[Window]) -> Dict[str, Any]:
    by_label: Dict[str, int] = {}
    by_scenario: Dict[str, int] = {}
    for window in windows:
        by_label[window.label] = by_label.get(window.label, 0) + 1
        by_scenario[window.scenario_id] = by_scenario.get(window.scenario_id, 0) + 1
    return {
        "total_windows": len(windows),
        "by_label": dict(sorted(by_label.items())),
        "by_scenario": dict(sorted(by_scenario.items())),
    }