"""Detectores determinísticos de padrões e loops."""

from .base import DetectorConfig, Finding  # noqa: F401
from .engine import DetectionEngine, detect  # noqa: F401

__all__ = ["DetectorConfig", "Finding", "DetectionEngine", "detect"]