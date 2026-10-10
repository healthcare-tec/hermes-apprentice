"""Camada de modelo local (Qwen3-0.6B) para classificação de janelas de eventos."""

from .dataset import Window, build_dataset  # noqa: F401
from .qwen import QwenClassifier  # noqa: F401

__all__ = ["Window", "build_dataset", "QwenClassifier"]