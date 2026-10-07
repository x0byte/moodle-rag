"""Local embeddings. The model runs on this machine; text is never sent anywhere."""

import logging
import os
from functools import lru_cache

import numpy as np

from . import config

log = logging.getLogger("moodle_rag.embed")

# bge models are trained with this instruction on the query side only.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


@lru_cache(maxsize=1)
def model():
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


def count_tokens(text: str) -> int:
    return len(model().tokenizer.encode(text, add_special_tokens=False))


def truncate_tokens(text: str, limit: int) -> str:
    tokenizer = model().tokenizer
    ids = tokenizer.encode(text, add_special_tokens=False)
    return text if len(ids) <= limit else tokenizer.decode(ids[:limit])


def embed_passages(texts: list[str]) -> np.ndarray:
    return model().encode(
        texts, normalize_embeddings=True, batch_size=32, show_progress_bar=False
    ).astype(np.float32)


def embed_query(query: str) -> np.ndarray:
    return model().encode(QUERY_PREFIX + query, normalize_embeddings=True).astype(np.float32)
