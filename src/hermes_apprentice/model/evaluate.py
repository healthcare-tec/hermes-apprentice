"""Avaliação do classificador local contra a linha de base determinística.

Duas medições, no mesmo conjunto de janelas rotuladas:

* **Linha de base** — os detectores determinísticos aplicados a cada janela.
* **Qwen3-0.6B** — o mesmo julgamento feito pelo modelo.

Comparar as duas é o ponto do exercício: se o modelo pequeno não supera regras
explícitas, o relatório precisa dizer isso, e não esconder atrás de uma métrica
agregada favorável.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..detectors.engine import DetectionEngine
from .dataset import Window

CLASSES = ("normal", "loop", "failure")


def confusion_matrix(y_true: Sequence[str], y_pred: Sequence[str], labels: Sequence[str] = CLASSES) -> List[List[int]]:
    index = {label: i for i, label in enumerate(labels)}
    matrix = [[0] * len(labels) for _ in labels]
    for truth, pred in zip(y_true, y_pred):
        if truth in index and pred in index:
            matrix[index[truth]][index[pred]] += 1
    return matrix


def classification_metrics(y_true: Sequence[str], y_pred: Sequence[str], labels: Sequence[str] = CLASSES) -> Dict[str, Any]:
    """Acurácia, precisão/revogação/F1 por classe e F1 macro."""
    matrix = confusion_matrix(y_true, y_pred, labels)
    total = sum(sum(row) for row in matrix)
    correct = sum(matrix[i][i] for i in range(len(labels)))
    per_class: Dict[str, Dict[str, float]] = {}
    for i, label in enumerate(labels):
        tp = matrix[i][i]
        fp = sum(matrix[r][i] for r in range(len(labels)) if r != i)
        fn = sum(matrix[i][c] for c in range(len(labels)) if c != i)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        support = sum(matrix[i])
        per_class[label] = {
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "f1": round(f1, 3),
            "support": support,
        }
    macro_f1 = sum(v["f1"] for v in per_class.values()) / len(labels)
    return {
        "accuracy": round(correct / total, 3) if total else 0.0,
        "macro_f1": round(macro_f1, 3),
        "per_class": per_class,
        "confusion_matrix": matrix,
        "labels": list(labels),
        "n": total,
    }


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[k]


def run_baseline(windows: Sequence[Window]) -> List[Dict[str, Any]]:
    """Aplica os detectores determinísticos a cada janela (isoladamente)."""
    engine = DetectionEngine()
    results: List[Dict[str, Any]] = []
    for window in windows:
        started = time.monotonic()
        detection = engine.run(window.events)
        elapsed = time.monotonic() - started
        verdict = "normal"
        rules = [f.rule for f in detection.findings]
        if any(rule.startswith("failure.") for rule in rules):
            verdict = "failure"
        elif any(rule.startswith("loop.") for rule in rules):
            verdict = "loop"
        results.append(
            {
                "window": window.to_dict(),
                "predicted": verdict,
                "truth": window.label,
                "rules": rules,
                "latency_s": round(elapsed, 5),
            }
        )
    return results


def run_model(classifier: Any, windows: Sequence[Window], *, max_events: int = 16,
              progress: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Executa o classificador em cada janela, medindo latência e tokens."""
    results: List[Dict[str, Any]] = []
    for position, window in enumerate(windows, start=1):
        prediction = classifier.classify(window.events[:max_events])
        results.append({"window": window.to_dict(), "truth": window.label, **prediction.to_dict()})
        if progress is not None:
            progress(position, len(windows), window, prediction)
    return results


def summarize_model(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    latencies = [r["latency_s"] for r in results]
    tps = [r["tokens_per_second"] for r in results if r.get("tokens_per_second")]
    metrics = classification_metrics([r["truth"] for r in results], [r["label"] for r in results])
    return {
        **metrics,
        "parse_failures": sum(1 for r in results if not r.get("parse_ok")),
        "latency_s": {
            "mean": round(statistics.mean(latencies), 3) if latencies else 0.0,
            "median": round(statistics.median(latencies), 3) if latencies else 0.0,
            "p95": round(percentile(latencies, 95), 3),
            "max": round(max(latencies), 3) if latencies else 0.0,
            "total": round(sum(latencies), 2),
        },
        "throughput_tokens_per_second": {
            "mean": round(statistics.mean(tps), 1) if tps else 0.0,
            "min": round(min(tps), 1) if tps else 0.0,
            "max": round(max(tps), 1) if tps else 0.0,
        },
        "prompt_tokens_mean": round(statistics.mean([r["prompt_tokens"] for r in results]), 1)
        if results
        else 0.0,
    }


def summarize_baseline(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    latencies = [r["latency_s"] for r in results]
    metrics = classification_metrics([r["truth"] for r in results], [r["predicted"] for r in results])
    return {
        **metrics,
        "rule_hits": _rule_counts(results),
        "latency_s": {
            "mean": round(statistics.mean(latencies), 5) if latencies else 0.0,
            "total": round(sum(latencies), 4),
        },
    }


def _rule_counts(results: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for result in results:
        for rule in result.get("rules", []):
            counts[rule] = counts.get(rule, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def error_analysis(model_results: Sequence[Dict[str, Any]], limit: int = 12) -> List[Dict[str, Any]]:
    """Lista os erros do modelo com o motivo declarado — base qualitativa do relatório."""
    errors = [r for r in model_results if r["label"] != r["truth"]]
    errors.sort(key=lambda r: (r["truth"], r["window"]["scenario_id"]))
    return [
        {
            "scenario_id": r["window"]["scenario_id"],
            "truth": r["truth"],
            "predicted": r["label"],
            "loop_type": r.get("loop_type"),
            "reason": r.get("reason"),
            "parse_ok": r.get("parse_ok"),
        }
        for r in errors[:limit]
    ]