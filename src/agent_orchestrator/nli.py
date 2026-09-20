"""NLI cross-encoder served through ONNX Runtime instead of PyTorch.

Same model as before (`cross-encoder/nli-deberta-v3-base`), exported and
dynamically quantised to int8 at build time. That keeps the published claim
about the architecture intact while removing torch from the runtime image and
shrinking the weights from ~700 MB to ~180 MB.
"""
import os
from functools import lru_cache
from typing import List, Sequence, Tuple

import numpy as np

MODEL_DIR = os.environ.get("NLI_MODEL_DIR", "/models/nli-onnx")
MAX_LENGTH = int(os.environ.get("NLI_MAX_LENGTH", "256"))

# cross-encoder/nli-deberta-v3-base label order.
LABELS = {0: "contradiction", 1: "entailment", 2: "neutral"}


class NLIUnavailable(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _load():
    try:
        import onnxruntime as ort
        from transformers import AutoTokenizer
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise NLIUnavailable(
            "Faltan onnxruntime y/o transformers en el entorno de ejecución."
        ) from exc

    model_path = os.path.join(MODEL_DIR, "model.onnx")
    if not os.path.exists(model_path):
        raise NLIUnavailable(
            f"No se encuentra el modelo NLI en {model_path}. "
            "Ejecuta scripts/export_nli_onnx.py o reconstruye la imagen."
        )

    options = ort.SessionOptions()
    # One thread per core is counter-productive on a shared free-tier CPU.
    options.intra_op_num_threads = int(os.environ.get("ONNX_THREADS", "2"))
    session = ort.InferenceSession(model_path, options, providers=["CPUExecutionProvider"])
    tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR)
    expected = {i.name for i in session.get_inputs()}
    return session, tokenizer, expected


def _softmax(logits: np.ndarray) -> np.ndarray:
    shifted = logits - logits.max(axis=-1, keepdims=True)
    exp = np.exp(shifted)
    return exp / exp.sum(axis=-1, keepdims=True)


def predict(pairs: Sequence[Tuple[str, str]]) -> List[dict]:
    """Score (premise, hypothesis) pairs. Returns label + confidence per pair."""
    if not pairs:
        return []

    session, tokenizer, expected = _load()
    premises = [p for p, _ in pairs]
    hypotheses = [h for _, h in pairs]

    encoded = tokenizer(
        premises,
        hypotheses,
        padding=True,
        truncation=True,
        max_length=MAX_LENGTH,
        return_tensors="np",
    )
    feed = {k: v.astype(np.int64) for k, v in encoded.items() if k in expected}
    logits = session.run(None, feed)[0]
    probabilities = _softmax(logits)

    results = []
    for row in probabilities:
        index = int(row.argmax())
        results.append({
            "label": LABELS.get(index, "unknown"),
            "confidence": float(row[index]),
        })
    return results


def is_available() -> bool:
    try:
        _load()
        return True
    except Exception:  # noqa: BLE001 - availability probe must never raise
        return False
