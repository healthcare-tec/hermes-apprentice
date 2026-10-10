"""Testes de redação de segredos.

Se algum destes falhar, o coletor está a um passo de gravar credenciais em disco.
"""

import pytest

from hermes_apprentice.redact import REDACTED, is_secret_path, redact_mapping, redact_text


@pytest.mark.parametrize(
    "text",
    [
        "OPENAI_API_KEY=sk-proj-abcdef123456",
        "export GITHUB_TOKEN=ghp_abcdefghijklmnop",
        "token: AIzaSyA1b2C3d4E5f6G7h",
        "Authorization: Bearer abcdefghijklmnopqrst",
        "hf_abcdefghijklmnopqrst",
        "slack xoxb-1234567890-abcdef",
    ],
)
def test_secrets_are_redacted(text):
    out = redact_text(text)
    assert REDACTED in out
    assert "abcdef123456" not in out
    assert "ghp_abcdefghijklmnop" not in out


def test_plain_text_is_preserved():
    text = "pytest tests/test_api.py -q"
    assert redact_text(text) == text


def test_truncation():
    out = redact_text("x" * 900, max_len=100)
    assert len(out) <= 100 + len("…[truncado]")
    assert out.endswith("…[truncado]")


def test_secret_paths():
    assert is_secret_path("/home/u/.hermes/.env")
    assert is_secret_path("auth.json")
    assert not is_secret_path("config.yaml")


def test_mapping_redaction():
    data = {
        "model": "nous/qwen3-0.6b",
        "OPENROUTER_API_KEY": "sk-or-abcdefghijkl",
        "nested": {"TELEGRAM_TOKEN": "123456:abcdef", "profiles": ["cli", "gateway"]},
    }
    out = redact_mapping(data)
    assert out["model"] == "nous/qwen3-0.6b"
    assert out["OPENROUTER_API_KEY"] == REDACTED
    assert out["nested"]["TELEGRAM_TOKEN"] == REDACTED
    assert out["nested"]["profiles"] == ["cli", "gateway"]