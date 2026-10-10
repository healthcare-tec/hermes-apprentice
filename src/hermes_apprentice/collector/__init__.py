"""Coletor de eventos em modo somente observação."""

from .pipeline import CollectorPipeline, collect_from  # noqa: F401

__all__ = ["CollectorPipeline", "collect_from"]