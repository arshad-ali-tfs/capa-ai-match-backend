"""Stable MATCH v1 primitives, independent of any external model or vector DB."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
from html import unescape
from threading import Lock
from typing import Iterable

import numpy as np

DEFAULT_MODEL = 'sentence-transformers/all-mpnet-base-v2'
MODEL_DIM = 768
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50
WEIGHTS = (0.70, 0.20, 0.10)  # semantic, BM25, Jaccard
TOKEN_PATTERN = re.compile(r"[a-z0-9]+(?:['-][a-z0-9]+)*", flags=re.IGNORECASE)


def normalize_pr_id(value: object) -> str:
    """Normalize GTW IDs (typically '1234' or '1234.0') without guessing names."""
    s = str(value).strip()
    if not s or s.lower() in {'nan', 'none', 'null'}:
        return ''
    if re.fullmatch(r'\d+\.0+', s):
        return str(int(s.split('.', 1)[0]))
    if re.fullmatch(r'\d+', s):
        return str(int(s))
    return s


TEXT_NORMALIZATION_VERSION = 'scientific-text-v2'
# Only actual, known markup is removed. A generic <...> pattern destroys
# specifications such as "<0.10% ... -->" and angle-wrapped notifications.
HTML_TAG = re.compile(r'</?(?:html|body|div|span|p|br|hr|b|strong|i|em|u|s|sup|sub|font|a|ul|ol|li|table|thead|tbody|tfoot|tr|td|th|pre|code|blockquote|h[1-6])(?:\s+[A-Za-z_:][^<>]*?)?\s*/?>', re.I)


def clean_for_index(text: object) -> str:
    """Keep source meaning and numbers; normalize whitespace and HTML line breaks."""
    if text is None:
        return ''
    s = str(text)
    s = re.sub(r'<\s*br\s*/?>', '\n', s, flags=re.IGNORECASE)
    s = HTML_TAG.sub(' ', s)
    s = unescape(s)
    s = s.replace('\x00', ' ')
    s = re.sub(r'[ \t\r\f\v]+', ' ', s)
    s = re.sub(r'\n\s*\n+', '\n', s)
    return s.strip()


def tokenize(text: str) -> list[str]:
    return TOKEN_PATTERN.findall(text.lower())


def jaccard(query_tokens: Iterable[str], doc_tokens: Iterable[str]) -> float:
    left, right = set(query_tokens), set(doc_tokens)
    union = left | right
    return float(len(left & right) / len(union)) if union else 0.0


def unit_rows(vectors: np.ndarray) -> np.ndarray:
    arr = np.asarray(vectors, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    if arr.ndim != 2 or not np.all(np.isfinite(arr)):
        raise ValueError('Embeddings must be a finite 2-D array')
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    if np.any(norms < 1e-10):
        raise ValueError('Zero-norm embeddings are not allowed')
    return np.ascontiguousarray(arr / norms, dtype=np.float32)


def weighted_score(semantic: float, bm25: float, token_overlap: float) -> float:
    """All input component scores are [0, 1]. Jaccard is NOT inverted."""
    a, b, c = (max(0.0, min(1.0, float(s))) for s in (semantic, bm25, token_overlap))
    return WEIGHTS[0] * a + WEIGHTS[1] * b + WEIGHTS[2] * c


def split_chunks(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Character windows with exactly `overlap` shared characters.

    Normalize the source once, then preserve each window verbatim so no text is
    lost at boundaries. The final window can be shorter than `size`.
    """
    if size < 50 or not 0 <= overlap < size:
        raise ValueError('chunk size must be >=50; overlap must be >=0 and < size')
    text = clean_for_index(text)
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks = []
    for start in range(0, len(text), size - overlap):
        end = min(len(text), start + size)
        chunks.append(text[start:end])
        if end >= len(text):
            break
    return chunks


@dataclass
class BM25:
    """Okapi BM25, positive Robertson IDF, scoped to eligible chunks only."""
    docs: list[list[str]]
    k1: float = 1.5
    b: float = 0.75

    def __post_init__(self) -> None:
        self.n = len(self.docs)
        self.lengths = [len(doc) for doc in self.docs]
        self.avg_length = sum(self.lengths) / self.n if self.n else 1.0
        self.postings: dict[str, list[tuple[int, int]]] = {}
        for i, doc in enumerate(self.docs):
            for term, freq in Counter(doc).items():
                self.postings.setdefault(term, []).append((i, freq))

    def scores(self, query: list[str]) -> np.ndarray:
        scores = np.zeros(self.n, dtype=np.float64)
        if not self.n:
            return scores
        for word in set(query):
            matches = self.postings.get(word, [])
            if not matches:
                continue
            idf = math.log1p((self.n - len(matches) + 0.5) / (len(matches) + 0.5))
            for index, freq in matches:
                scale = self.k1 * (1 - self.b + self.b * self.lengths[index] / max(1, self.avg_length))
                scores[index] += idf * (freq * (self.k1 + 1)) / (freq + scale)
        return scores


class MockEmbedder:
    """Deterministic hashed token vectors for OFFLINE DEMOS/UNIT TESTS ONLY.

    These are not MPNet embeddings and cannot demonstrate semantic quality.
    """
    model_name = 'MOCK-hashed-token-embeddings-NOT-MPNET'
    dim = MODEL_DIM

    def encode(self, texts: list[str]) -> np.ndarray:
        data = np.zeros((len(texts), MODEL_DIM), dtype=np.float32)
        for row, text in enumerate(texts):
            toks = tokenize(text) or ['_empty_']
            for token in toks:
                digest = hashlib.blake2b(token.encode('utf-8'), digest_size=8).digest()
                idx = int.from_bytes(digest[:4], 'little') % MODEL_DIM
                data[row, idx] += 1.0
        return unit_rows(data)


class MPNetEmbedder:
    """Real SentenceTransformer model. Loads once on first use."""
    dim = MODEL_DIM
    runtime = 'torch'

    def __init__(self, model_name: str = DEFAULT_MODEL, batch_size: int = 16):
        self.model_name = model_name
        self.batch_size = batch_size
        self._model = None
        self._load_lock = Lock()

    def _load(self):
        with self._load_lock:
            if self._model is None:
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError as exc:
                    raise RuntimeError('Install requirements.txt for the real MPNet model') from exc
                cache = os.environ.get('MATCH_MODEL_CACHE',
                                       str(Path(__file__).resolve().parent.parent / '.cache' / 'models'))
                self._model = SentenceTransformer(self.model_name, cache_folder=cache)
        return self._model

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, MODEL_DIM), dtype=np.float32)
        arr = self._load().encode(texts, normalize_embeddings=True,
                                 convert_to_numpy=True, batch_size=self.batch_size,
                                 show_progress_bar=False)
        arr = unit_rows(arr)
        if arr.shape[1] != MODEL_DIM:
            raise ValueError(f'Expected 768D MPNet embedding; got {arr.shape[1]}D')
        return arr


class ONNXMPNetEmbedder:
    """Official float32 MPNet O2 export with attention-mask mean pooling.

    Uses the same base checkpoint, tokenizer and 384-token limit as the torch
    encoder. No quantization is used. The index records its runtime explicitly.
    """
    dim = MODEL_DIM
    runtime = 'onnx-fp32'

    def __init__(self, model_name: str = DEFAULT_MODEL, batch_size: int = 32):
        self.model_name = model_name
        self.batch_size = batch_size
        self._session = None
        self._tokenizer = None
        self._load_lock = Lock()

    def _load(self):
        with self._load_lock:
            if self._session is not None:
                return
            import onnxruntime as ort
            from transformers import AutoTokenizer
            from huggingface_hub import hf_hub_download
            from huggingface_hub.errors import LocalEntryNotFoundError
            cache = os.environ.get('MATCH_MODEL_CACHE',
                                   str(Path(__file__).resolve().parent.parent / '.cache' / 'models'))
            def artifact(name):
                if Path(self.model_name).is_dir():
                    return str(Path(self.model_name) / name)
                try:
                    return hf_hub_download(self.model_name, filename=name, cache_dir=cache, local_files_only=True)
                except LocalEntryNotFoundError:
                    return hf_hub_download(self.model_name, filename=name, cache_dir=cache)
            pooling = json.loads(Path(artifact('1_Pooling/config.json')).read_text(encoding='utf-8'))
            modes = [name for name, enabled in pooling.items() if name.startswith('pooling_mode_') and enabled]
            if modes != ['pooling_mode_mean_tokens']:
                raise ValueError('ONNX runtime requires the base MPNet mean-pooling configuration')
            config = json.loads(Path(artifact('sentence_bert_config.json')).read_text(encoding='utf-8'))
            self.max_length = int(config['max_seq_length'])
            snapshot = Path(artifact('config.json')).parent
            self._tokenizer = AutoTokenizer.from_pretrained(str(snapshot), local_files_only=True)
            options = ort.SessionOptions()
            options.intra_op_num_threads = min(4, os.cpu_count() or 1)
            options.inter_op_num_threads = 1
            self._session = ort.InferenceSession(artifact('onnx/model_O2.onnx'),
                                                sess_options=options,
                                                providers=['CPUExecutionProvider'])

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, MODEL_DIM), dtype=np.float32)
        self._load()
        output = []
        for start in range(0, len(texts), self.batch_size):
            batch = self._tokenizer(texts[start:start+self.batch_size], padding=True,
                                    truncation=True, max_length=self.max_length, return_tensors='np')
            inputs = {item.name: np.asarray(batch[item.name], dtype=np.int64)
                      for item in self._session.get_inputs()}
            hidden = self._session.run(None, inputs)[0]
            mask = np.asarray(batch['attention_mask'], dtype=np.float32)[:, :, None]
            vectors = unit_rows((hidden * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1e-9))
            if vectors.shape != (len(batch['input_ids']), MODEL_DIM):
                raise ValueError('Expected 768D MPNet ONNX output')
            output.append(vectors)
        return np.concatenate(output)


def make_mpnet_embedder(model_name: str = DEFAULT_MODEL, batch_size: int = 16,
                       runtime: str = 'torch'):
    if runtime == 'torch':
        return MPNetEmbedder(model_name, batch_size)
    if runtime == 'onnx-fp32':
        return ONNXMPNetEmbedder(model_name, batch_size)
    raise ValueError(f'Unsupported MPNet runtime: {runtime}')
