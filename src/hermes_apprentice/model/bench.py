"""Bancada de avaliação: roda a linha de base e o modelo local no mesmo conjunto.

O resultado é um artefato JSON autocontido. Ele registra também o **ambiente**
(núcleos, RAM, dispositivo, dtype, tempo de carga dos pesos), porque um número de
latência sem o hardware ao lado não é comparável com nada.
"""

from __future__ import annotations

import json
import os
import platform
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .dataset import build_dataset, dataset_summary
from .evaluate import error_analysis, run_baseline, run_model, summarize_baseline, summarize_model
from .qwen import DEFAULT_MODEL_ID, QwenClassifier


def hardware_info(*, device: str, dtype: str, load_seconds: float) -> Dict[str, Any]:
    host_gb = _meminfo_total_gb()
    limit_gb = _cgroup_limit_gb()
    # Reportar só o MemTotal do host engana: em contêiner o processo morre por OOM
    # muito antes disso. Publicamos o efetivo e, ao lado, o do host.
    candidates = [value for value in (host_gb, limit_gb) if value is not None]
    effective = min(candidates) if candidates else None
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_cores": os.cpu_count(),
        "ram_gb": effective,
        "ram_host_gb": host_gb,
        "ram_cgroup_gb": limit_gb,
        "device": device,
        "dtype": dtype,
        "gpu": False,
        "load_seconds": round(load_seconds, 2),
    }


def _meminfo_total_gb() -> Optional[float]:
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / 1024 / 1024, 2)
    except OSError:
        pass
    return None


def _cgroup_limit_gb() -> Optional[float]:
    """Limite de memória do contêiner (cgroup v2 e v1), quando existir."""
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = open(path, "r", encoding="utf-8").read().strip()
        except OSError:
            continue
        if not raw or raw == "max":
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value <= 0 or value > 1 << 50:  # "sem limite" vem como número absurdo
            continue
        return round(value / 1024 ** 3, 2)
    return None


def run_benchmark(
    scenarios_dir: str | Path,
    out_path: str | Path,
    *,
    model_id: str = DEFAULT_MODEL_ID,
    window_size: int = 12,
    stride: int = 6,
    max_windows_per_zone: int = 3,
    max_events: int = 16,
    device: str = "cpu",
    threads: Optional[int] = None,
    local_files_only: bool = False,
    limit: Optional[int] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Executa a bancada completa e grava o artefato. Devolve o dicionário do resultado."""

    def log(message: str) -> None:
        if progress is not None:
            progress(message)

    log(f"[bench] construindo conjunto a partir de {scenarios_dir}")
    windows = build_dataset(
        scenarios_dir,
        window_size=window_size,
        stride=stride,
        max_windows_per_zone=max_windows_per_zone,
    )
    if limit:
        windows = windows[:limit]
    summary = dataset_summary(windows)
    log(f"[bench] {summary['total_windows']} janelas: {summary['by_label']}")

    log("[bench] linha de base (detectores determinísticos)")
    baseline_results = run_baseline(windows)
    baseline = summarize_baseline(baseline_results)

    log(f"[bench] carregando {model_id} em {device} (float32)")
    classifier = QwenClassifier(model_id, device=device, threads=threads, local_files_only=local_files_only)
    classifier.load()
    log(f"[bench] pesos carregados em {classifier.load_seconds:.1f}s")

    started = time.monotonic()

    def on_progress(position: int, total: int, window: Any, prediction: Any) -> None:
        log(
            f"[bench] {position}/{total} {window.scenario_id} "
            f"verdadeiro={window.label} previsto={prediction.label} "
            f"({prediction.latency_s:.1f}s, {prediction.tokens_per_second:.1f} tok/s, json_ok={prediction.parse_ok})"
        )

    model_results = run_model(classifier, windows, max_events=max_events, progress=on_progress)
    total_seconds = time.monotonic() - started

    result: Dict[str, Any] = {
        "schema_version": 1,
        "model_id": model_id,
        "dataset": {
            **summary,
            "window_size": window_size,
            "stride": stride,
            "max_windows_per_zone": max_windows_per_zone,
        },
        "hardware": hardware_info(device=device, dtype="float32", load_seconds=classifier.load_seconds),
        "baseline": baseline,
        "model": summarize_model(model_results),
        "model_windows": model_results,
        "baseline_windows": baseline_results,
        "errors": error_analysis(model_results),
        "timing": {"total_seconds": round(total_seconds, 2)},
    }

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[bench] artefato gravado em {out}")
    return result
