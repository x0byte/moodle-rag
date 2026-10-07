"""Local embeddings. The model runs on this machine; text is never sent anywhere."""

import logging
import os
import threading

import numpy as np

from . import config

log = logging.getLogger("moodle_rag.embed")

# bge models are trained with this instruction on the query side only.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


_model = None
_model_lock = threading.Lock()


def model():
    """The embedding model, loaded on first use. Safe to call from several threads."""
    global _model
    with _model_lock:
        if _model is None:
            _model = _load()
    return _model


def _load():
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    from sentence_transformers import SentenceTransformer
    from transformers.utils import logging as transformers_logging

    # Token counting runs on whole pages; silence the "longer than 512" warning.
    transformers_logging.set_verbosity_error()
    transformers_logging.disable_progress_bar()
    try:
        # Straight from the local cache: no network access at all.
        loaded = SentenceTransformer(config.EMBEDDING_MODEL, local_files_only=True)
    except Exception:
        log.info("downloading embedding model %s (first run only)", config.EMBEDDING_MODEL)
        loaded = SentenceTransformer(config.EMBEDDING_MODEL)
    log.info("embedding model: %s", config.EMBEDDING_MODEL)
    return loaded


# The model and its tokenizer are not safe to call from several threads at
# once (parallel tool calls, parallel uploads), so calls take turns.
_use_lock = threading.Lock()


def count_tokens(text: str) -> int:
    with _use_lock:
        return len(model().tokenizer.encode(text, add_special_tokens=False))


def truncate_tokens(text: str, limit: int) -> str:
    tokenizer = model().tokenizer
    with _use_lock:
        ids = tokenizer.encode(text, add_special_tokens=False)
        return text if len(ids) <= limit else tokenizer.decode(ids[:limit])


def embed_passages(texts: list[str]) -> np.ndarray:
    loaded = model()
    with _use_lock:
        vectors = loaded.encode(
            texts, normalize_embeddings=True, batch_size=32, show_progress_bar=False
        )
    return vectors.astype(np.float32)


def embed_query(query: str) -> np.ndarray:
    loaded = model()
    with _use_lock:
        vector = loaded.encode(QUERY_PREFIX + query, normalize_embeddings=True)
    return vector.astype(np.float32)
