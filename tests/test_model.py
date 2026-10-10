"""Testes do parser de respostas e das métricas de avaliação.

Estes dois módulos são o ponto onde é mais fácil mentir sem perceber: uma resposta
ilegível descartada do denominador transforma uma acurácia de 0,47 em 0,70. Os
testes abaixo fixam o comportamento correto.
"""

import pytest

from hermes_apprentice.model.evaluate import (
    CLASSES,
    classification_metrics,
    confusion_matrix,
    summarize_model,
)
from hermes_apprentice.model.qwen import balanced_objects, parse_prediction


# -- parser -----------------------------------------------------------------
def test_clean_json_is_strict():
    result = parse_prediction('{"label": "loop", "loop_type": "repetition", "reason": "x"}')
    assert result["label"] == "loop"
    assert result["parse_method"] == "strict"


def test_code_fence_is_strict():
    text = '```json\n{"label": "failure", "loop_type": "none", "reason": "y"}\n```'
    result = parse_prediction(text)
    assert result["label"] == "failure"
    assert result["parse_method"] == "strict"


def test_trailing_prose_is_strict():
    text = 'Claro! {"label": "loop", "reason": "repete"} Espero ter ajudado.'
    assert parse_prediction(text)["label"] == "loop"


def test_literal_newline_inside_string_is_repaired():
    """O caso real observado com o Qwen3-0.6B: quebra de linha dentro da string."""
    text = '{"label": "normal", "reason": "progresso\ncom várias chamadas"}'
    result = parse_prediction(text)
    assert result["parse_ok"] is True
    assert result["parse_method"] == "repaired"
    assert result["label"] == "normal"


def test_single_quotes_are_repaired():
    result = parse_prediction("{'label': 'loop', 'reason': 'b'}")
    assert result["parse_method"] == "repaired"


def test_braces_inside_strings_do_not_break_scan():
    text = '{"label": "normal", "reason": "usa {} em templates"}'
    result = parse_prediction(text)
    assert result["label"] == "normal"
    assert balanced_objects(text) == [text]


def test_unreadable_response_is_reported_as_none():
    for text in ("Não consegui classificar.", "", "   "):
        result = parse_prediction(text)
        assert result["parse_ok"] is False
        assert result["parse_method"] == "none"


@pytest.mark.parametrize(
    "text,expected",
    [
        # Corte exatamente no meio do campo `reason` — o caso real observado.
        (
            '{"label": "normal", "loop_type": "none", "confidence": 0.0, "reason": "The agent is making '
            'progress with multiple tool calls and successful results, indicating a healthy run."',
            "normal",
        ),
        (
            '{"label": "failure", "loop_type": "retry_storm", "confidence": 0.0, "reason": "repeated errors',
            "failure",
        ),
        ('{"label": "loop", "reason": "sem fecha', "loop"),
    ],
)
def test_truncated_object_is_salvaged_and_labelled(text, expected):
    """O rótulo está no fragmento; recuperá-lo é legítimo — desde que rotulado."""
    result = parse_prediction(text)
    assert result["label"] == expected
    assert result["parse_method"] == "truncated"


def test_unknown_label_is_normalized():
    result = parse_prediction('{"label": "talvez", "reason": "?"}')
    assert result["label"] == "unknown"


# -- métricas ---------------------------------------------------------------
def test_unreadable_answers_stay_in_the_denominator():
    """10 janelas ilegíveis não podem sumir da conta."""
    y_true = ["loop"] * 3 + ["normal"] * 2
    y_pred = ["unknown", "loop", "failure", "normal", "unknown"]
    metrics = classification_metrics(y_true, y_pred)
    assert metrics["n"] == 5, "o total precisa incluir as respostas ilegíveis"
    # As métricas são arredondadas em três casas — comparamos o número publicado.
    assert metrics["accuracy"] == 0.4
    assert metrics["accuracy_on_parsed"] == 0.667


def test_unreadable_counts_as_false_negative():
    metrics = classification_metrics(["loop"], ["unknown"])
    assert metrics["per_class"]["loop"]["recall"] == 0.0
    assert metrics["per_class"]["loop"]["f1"] == 0.0


def test_perfect_predictions_score_one():
    y = ["normal", "loop", "failure"]
    metrics = classification_metrics(y, y)
    assert metrics["accuracy"] == 1.0
    assert metrics["macro_f1"] == 1.0
    assert metrics["accuracy_on_parsed"] == 1.0


def test_confusion_matrix_has_an_unknown_column():
    matrix = confusion_matrix(["loop"], ["unknown"], list(CLASSES) + ["unknown"])
    assert matrix[1] == [0, 0, 0, 1]


def test_summarize_model_counts_parse_methods():
    results = [
        {"truth": "loop", "label": "loop", "parse_method": "strict", "parse_ok": True,
         "latency_s": 1.0, "tokens_per_second": 5.0, "prompt_tokens": 100},
        {"truth": "loop", "label": "normal", "parse_method": "repaired", "parse_ok": True,
         "latency_s": 2.0, "tokens_per_second": 4.5, "prompt_tokens": 100},
        {"truth": "normal", "label": "unknown", "parse_method": "none", "parse_ok": False,
         "latency_s": 3.0, "tokens_per_second": 4.0, "prompt_tokens": 100},
    ]
    summary = summarize_model(results)
    assert summary["parse_methods"] == {"strict": 1, "repaired": 1, "truncated": 0, "none": 1}
    assert summary["parse_failures"] == 1
    assert summary["n"] == 3
    assert summary["accuracy"] == 0.333
    assert summary["latency_s"]["mean"] == 2.0
