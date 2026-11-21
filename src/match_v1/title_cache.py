"""Reusable FP32 title vectors, tied to exact text and verified model assets."""
from functools import lru_cache
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from zipfile import BadZipFile
from threading import Lock
import numpy as np
from .core import unit_rows

LOG = logging.getLogger(__name__)
FORMAT = 'match-title-vectors-v1'


@lru_cache(maxsize=32)
def _asset_hash(path, size, modified):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def asset_hash(path):
    stat = path.stat()
    return _asset_hash(str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def model_fingerprint(service):
    embedder = service.embedder
    # A name alone cannot prove that a checkpoint has not changed. Other
    # runtimes safely retain ordinary in-memory title calculation.
    model_path = getattr(getattr(embedder, '_session', None), '_model_path', None)
    tokenizer = getattr(embedder, '_tokenizer', None)
    if not model_path or tokenizer is None or not Path(model_path).is_file():
        return None
    directory = Path(tokenizer.name_or_path)
    assets = {name: asset_hash(directory / name) for name in [
        'config.json', 'tokenizer.json', 'tokenizer_config.json', 'special_tokens_map.json',
        'vocab.txt', 'sentence_bert_config.json', '1_Pooling/config.json'] if (directory / name).is_file()}
    if not any(name in assets for name in ['tokenizer.json', 'vocab.txt']):
        return None
    signature = {'format': FORMAT, 'model': service.manifest['model_name'],
                 'runtime': service.manifest.get('embedding_runtime', 'torch'),
                 'maximum_length': getattr(embedder, 'max_length', None),
                 'weights': asset_hash(Path(model_path)), 'assets': assets}
    return hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()


def text_key(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class TitleVectors:
    def __init__(self, service, vectors, directory=None):
        self.service, self.vectors = service, vectors
        configured = directory or os.getenv('MATCH_TITLE_CACHE_DIR')
        self.directory = Path(configured) if configured else Path(os.getenv('CAPA_CACHE_DIR', './data/cache')) / 'match-title-vectors'
        self.lock = Lock()
        self.loaded = False
        self.fingerprint = None
        self.bank = {}

    def load(self):
        with self.lock:
            if self.loaded:
                return
            try:
                self.fingerprint = model_fingerprint(self.service)
            except (OSError, TypeError, ValueError, AttributeError):
                LOG.warning('MATCH title cache assets could not be verified; using original model calculation.')
            if self.fingerprint:
                path = self.directory / (self.fingerprint + '.npz')
                if path.is_file():
                    try:
                        with np.load(path, allow_pickle=False) as archive:
                            keys, vectors = archive['keys'], archive['vectors']
                            metadata = json.loads(str(archive['metadata'].item()))
                        if (not isinstance(metadata, dict) or metadata.get('format') != FORMAT or metadata.get('model') != self.fingerprint
                                or keys.ndim != 1 or len(keys) > 200000 or vectors.shape != (len(keys), 768)
                                or vectors.dtype != np.float32 or keys.dtype.kind != 'U'
                                or len(set(keys.tolist())) != len(keys)
                                or any(not re.fullmatch(r'[a-f0-9]{64}', key) for key in keys.tolist())
                                or not np.isfinite(vectors).all()
                                or not np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=.003)
                                or metadata.get('content') != hashlib.sha256(keys.tobytes() + vectors.tobytes()).hexdigest()):
                            raise ValueError('Invalid title cache')
                        self.bank = dict(zip(keys.tolist(), vectors))
                    except (OSError, ValueError, KeyError, TypeError, EOFError, BadZipFile):
                        LOG.warning('Ignoring an invalid MATCH title cache; original model calculation remains available.')
            for key, record in self.service.records.items():
                title = record.get('record_name', '').strip()
                vector = self.bank.get(text_key(title)) if title else None
                if vector is not None:
                    self.vectors[(key, title)] = vector
            self.loaded = True

    def ensure(self, titles):
        self.load()
        with self.lock:
            missing = [(key, title) for key, title in titles.items() if (key, title) not in self.vectors]
        if not missing:
            return
        vectors = unit_rows(self.service.embedder.encode([title for _, title in missing]))
        if vectors.shape != (len(missing), 768) or not np.isfinite(vectors).all():
            raise RuntimeError('Event-title embeddings do not match the query model.')
        with self.lock:
            for (key, title), vector in zip(missing, vectors):
                self.vectors[(key, title)] = vector
                self.bank[text_key(title)] = vector

    def save(self):
        if not self.fingerprint:
            return False
        with self.lock:
            keys = np.asarray(sorted(self.bank), dtype='U64')
            vectors = np.stack([self.bank[key] for key in keys]).astype(np.float32) if len(keys) else np.empty((0, 768), dtype=np.float32)
        metadata = {'format': FORMAT, 'model': self.fingerprint,
                    'content': hashlib.sha256(keys.tobytes() + vectors.tobytes()).hexdigest()}
        temporary = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.directory, suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                np.savez(stream, keys=keys, vectors=vectors, metadata=np.asarray(json.dumps(metadata)))
            os.replace(temporary, self.directory / (self.fingerprint + '.npz'))
            return True
        except OSError:
            LOG.warning('MATCH title vectors remain available in memory; the optional disk cache could not be saved.')
            return False
        finally:
            if temporary and temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def precompute(self, progress=None):
        self.load()
        titles = {key: row.get('record_name', '').strip() for key, row in self.service.records.items() if row.get('record_name', '').strip()}
        pending = [(key, title) for key, title in titles.items() if (key, title) not in self.vectors]
        for offset in range(0, len(pending), 64):
            self.ensure(dict(pending[offset:offset + 64]))
            if offset % 1024 == 0:
                self.save()
                if progress:
                    progress(min(offset + 64, len(pending)), len(pending))
        if pending:
            self.save()
        return {'titles': len(titles), 'computed': len(pending), 'reused': len(titles) - len(pending)}
