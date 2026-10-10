"""Sonda de ambiente do Hermes (somente leitura)."""

from .environment import probe_environment, resolve_hermes_home  # noqa: F401

__all__ = ["probe_environment", "resolve_hermes_home"]