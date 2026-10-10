"""Classificador local com Qwen3-0.6B.

Por que 0.6B: o objetivo do "aprendiz" é rodar **junto** ao harness, na mesma
máquina, sem GPU e sem custo de API. Um modelo desse tamanho cabe em ~2,5 GB de
RAM em float32 e responde em segundos por janela em CPU. A contrapartida é
capacidade limitada — e é exatamente por isso que este projeto mede o desempenho
em vez de assumi-lo (ver ``evaluate.py`` e ``reports/``).

O modelo é carregado uma única vez por processo. Nenhuma chamada de rede é feita
após o download inicial dos pesos.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from ..schema import Event
from .prompts import LABELS, LOOP_TYPES, build_messages

DEFAULT_MODEL_ID = "Qwen/Qwen3-0.6B"

_JSON_RE = re.compile(r"\{.*?\}", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?(.*?)```", re.DOTALL)


def balanced_objects(text: str) -> List[str]:
    """Todos os trechos ``{...}`` balanceados, do mais longo para o mais curto.

    Varredura por contagem de chaves, e não por expressão regular: regex não dão
    conta de chaves dentro de strings (``{"reason": "usei {} em template"}``).
    """
    objects: List[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    objects.append(text[start : index + 1])
    return sorted(objects, key=len, reverse=True)



def salvage_truncated(text: str) -> Optional[Dict[str, Any]]:
    """Recupera um objeto JSON **cortado no meio** pela janela de geração.

    Foi o modo de falha dominante do Qwen3-0.6B na primeira medição: com uma
    janela de geração curta, o modelo escreve ``label``, ``loop_type`` e
    ``confidence`` corretamente e é cortado no meio do campo ``reason``, deixando
    o objeto sem fechar. O rótulo — que é o que se classifica — está lá.

    A recuperação corta o fragmento na última vírgula e fecha a chave. O resultado
    recebe o método ``truncated``, nunca ``strict``: quem lê as métricas precisa
    saber que a resposta veio incompleta.
    """
    start = text.find("{")
    if start < 0:
        return None
    fragment = text[start:].rstrip()
    attempts = [fragment + '"}', fragment + "}"]
    for position in reversed([i for i, char in enumerate(fragment) if char == ","]):
        attempts.append(fragment[:position] + '"}')
        attempts.append(fragment[:position] + "}")
    for candidate in attempts:
        try:
            data = json.loads(candidate, strict=False)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "label" in data:
            return data
    return None


@dataclass
class Prediction:
    """Resposta do modelo para uma janela."""

    label: str = "unknown"
    loop_type: str = "none"
    confidence: Optional[float] = None
    reason: str = ""
    raw: str = ""
    latency_s: float = 0.0
    prompt_tokens: int = 0
    generated_tokens: int = 0
    parse_ok: bool = False
    #: ``strict`` | ``repaired`` | ``none`` — ver :func:`parse_prediction`.
    parse_method: str = "none"

    @property
    def tokens_per_second(self) -> float:
        return self.generated_tokens / self.latency_s if self.latency_s > 0 else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "loop_type": self.loop_type,
            "confidence": self.confidence,
            "reason": self.reason,
            "latency_s": round(self.latency_s, 3),
            "prompt_tokens": self.prompt_tokens,
            "generated_tokens": self.generated_tokens,
            "tokens_per_second": round(self.tokens_per_second, 1),
            "parse_ok": self.parse_ok,
            "parse_method": self.parse_method,
            "raw": self.raw[:400],
        }


def parse_prediction(text: str) -> Dict[str, Any]:
    """Extrai o JSON da resposta e informa **como** conseguiu.

    ``parse_method`` é o campo que importa para a honestidade do relatório:

    * ``strict``   — ``json.loads`` direto sobre o objeto balanceado;
    * ``repaired`` — só passou depois de ``strict=False`` (o modelo insere
      quebras de linha literais dentro das strings, o que o JSON padrão recusa)
      ou da normalização de aspas simples;
    * ``none``     — nenhum objeto utilizável; a janela conta como ``unknown``.

    Sem essa distinção, uma falha de *formato* seria relatada como falha de
    *julgamento* — exatamente o erro que este projeto existe para evitar.
    """
    raw = (text or "").strip()
    if not raw:
        return {"parse_ok": False, "parse_method": "none"}

    candidates: List[str] = []
    for fence in _FENCE_RE.findall(raw):
        candidates.extend(balanced_objects(fence.strip()))
    candidates.extend(balanced_objects(raw))
    if not candidates:
        match = _JSON_RE.search(raw)
        if match:
            candidates.append(match.group(0))

    data: Optional[Dict[str, Any]] = None
    method = "none"
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and "label" in parsed:
                data, method = parsed, "strict"
                break
        except json.JSONDecodeError:
            pass
        for tolerant in (candidate, candidate.replace("'", '"')):
            try:
                parsed = json.loads(tolerant, strict=False)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and "label" in parsed:
                data, method = parsed, "repaired"
                break
        if data is not None:
            break

    if data is None:
        salvaged = salvage_truncated(raw)
        if salvaged is not None:
            data, method = salvaged, "truncated"

    if data is None:
        return {"parse_ok": False, "parse_method": "none"}

    label = str(data.get("label", "unknown")).strip().lower()
    if label not in LABELS:
        label = "unknown"
    loop_type = str(data.get("loop_type", "none")).strip().lower()
    if loop_type not in LOOP_TYPES:
        loop_type = "none"
    confidence = data.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    return {
        "label": label,
        "loop_type": loop_type,
        "confidence": confidence,
        "reason": str(data.get("reason", ""))[:200],
        "parse_ok": True,
        "parse_method": method,
    }


class QwenClassifier:
    """Envelope fino sobre ``transformers`` para classificar janelas de eventos."""

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL_ID,
        *,
        device: str = "cpu",
        # 48 tokens cortavam o objeto JSON no meio do campo ``reason`` em cerca de
        # um terço das janelas. Como a geração para no EOS, elevar o teto não
        # encarece as respostas que já terminavam sozinhas.
        max_new_tokens: int = 96,
        enable_thinking: bool = False,
        threads: Optional[int] = None,
        local_files_only: bool = False,
    ):
        self.model_id = model_id
        self.device = device
        self.max_new_tokens = max_new_tokens
        self.enable_thinking = enable_thinking
        self.threads = threads
        self.local_files_only = local_files_only
        self._model = None
        self._tokenizer = None
        self.load_seconds: float = 0.0

    # -- carga -------------------------------------------------------------
    def load(self) -> "QwenClassifier":
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if self.threads:
            torch.set_num_threads(self.threads)
        started = time.monotonic()
        self._tokenizer = AutoTokenizer.from_pretrained(
            self.model_id, local_files_only=self.local_files_only
        )
        kwargs: Dict[str, Any] = {"local_files_only": self.local_files_only}
        try:  # transformers >= 5 renomeou torch_dtype -> dtype
            self._model = AutoModelForCausalLM.from_pretrained(self.model_id, dtype=torch.float32, **kwargs)
        except TypeError:
            self._model = AutoModelForCausalLM.from_pretrained(self.model_id, torch_dtype=torch.float32, **kwargs)
        self._model.to(self.device)
        self._model.eval()
        if self._tokenizer.pad_token_id is None:
            self._tokenizer.pad_token_id = self._tokenizer.eos_token_id
        self.load_seconds = time.monotonic() - started
        return self

    @property
    def loaded(self) -> bool:
        return self._model is not None and self._tokenizer is not None

    # -- inferência --------------------------------------------------------
    def classify(self, events: Sequence[Event], *, start: int = 0, max_events: int = 16) -> Prediction:
        import torch

        if not self.loaded:
            self.load()
        messages = build_messages(events, start=start, max_events=max_events)
        prompt = self._tokenizer.apply_chat_template(  # type: ignore[union-attr]
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=self.enable_thinking,
        )
        inputs = self._tokenizer(prompt, return_tensors="pt").to(self.device)  # type: ignore[operator]
        started = time.monotonic()
        with torch.no_grad():
            output = self._model.generate(  # type: ignore[union-attr]
                **inputs,
                max_new_tokens=self.max_new_tokens,
                do_sample=False,
                temperature=None,
                top_p=None,
                top_k=None,
                pad_token_id=self._tokenizer.pad_token_id,  # type: ignore[union-attr]
            )
        latency = time.monotonic() - started
        generated = output[0][inputs["input_ids"].shape[-1] :]
        text = self._tokenizer.decode(generated, skip_special_tokens=True)  # type: ignore[union-attr]
        parsed = parse_prediction(text)
        return Prediction(
            label=parsed.get("label", "unknown"),
            loop_type=parsed.get("loop_type", "none"),
            confidence=parsed.get("confidence"),
            reason=parsed.get("reason", ""),
            raw=text.strip(),
            latency_s=latency,
            prompt_tokens=int(inputs["input_ids"].shape[-1]),
            generated_tokens=int(generated.shape[-1]),
            parse_ok=bool(parsed.get("parse_ok")),
            parse_method=str(parsed.get("parse_method", "none")),
        )

    def smoke_test(self) -> Dict[str, Any]:
        """Teste mínimo de sanidade: uma janela com loop óbvio deve sair como ``loop``."""
        events = [
            Event(type="tool_call_start", tool="read_file", args_digest="aaaa1111"),
            Event(type="tool_call_end", tool="read_file", result_digest="bbbb2222", status="ok"),
        ] * 4
        prediction = self.classify(events, max_events=8)
        return {"prediction": prediction.to_dict(), "load_seconds": round(self.load_seconds, 2)}
