"""Testes da sonda de ambiente (sem rede e sem escrever nada)."""

import json
from pathlib import Path

from hermes_apprentice.probe import probe_environment, resolve_hermes_home
from hermes_apprentice.probe.environment import read_config, inspect_state_db


def test_resolution_order():
    assert resolve_hermes_home("/tmp/x", env={"HERMES_HOME": "/tmp/y"})["source"] == "explicit"
    assert resolve_hermes_home(env={"HERMES_HOME": "/tmp/y"})["path"] == "/tmp/y"
    default = resolve_hermes_home(env={})
    assert default["source"] == "platform_default"


def test_probe_is_json_serializable_and_offline(fixture_home: Path):
    report = probe_environment(
        str(fixture_home), hermes_bin=None, invoke_cli=False, network=False
    )
    json.dumps(report)  # não pode levantar
    assert report["hermes_home"]["exists"] is True
    assert report["config"]["exists"] is True
    assert report["state_db"]["exists"] is True
    assert report["state_db"]["read_only"] is True
    assert report["state_db"]["counts"]["sessions"] == 1


def test_probe_handles_missing_home(tmp_path: Path):
    report = probe_environment(str(tmp_path / "nada"), invoke_cli=False, network=False)
    assert report["hermes_home"]["exists"] is False
    assert report["config"]["exists"] is False
    assert report["state_db"]["exists"] is False
    assert report["notes"]


def test_read_config_redacts_secrets(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "model: nous/qwen3-0.6b\nterminal:\n  backend: local\nOPENROUTER_API_KEY: sk-or-abcdefghijkl\n",
        encoding="utf-8",
    )
    info = read_config(config)
    assert info["exists"] is True
    assert info["values"]["model"] == "nous/qwen3-0.6b"
    assert "sk-or-abcdefghijkl" not in json.dumps(info)
    assert info["highlights"]["terminal_backend"] == "local"


def test_inspect_state_db_never_writes(fixture_home: Path):
    info = inspect_state_db(fixture_home / "state.db")
    assert info["read_only"] is True
    assert "sessions" in info["tables"]
    # A sonda não pode deixar a tabela de teste criada.
    assert "_apprentice_probe" not in info["tables"]