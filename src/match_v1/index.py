"""Persist GTW metadata and normalized 768D vectors in local ChromaDB."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4
import time

import numpy as np

from .core import CHUNK_OVERLAP, CHUNK_SIZE, DEFAULT_MODEL, MockEmbedder, make_mpnet_embedder, split_chunks, unit_rows

COLLECTION = 'capa_match_mpnet_v1'


def storage_path(index_dir: str | Path, manifest: dict) -> Path:
    """Resolve one immutable index generation; support original v1 indexes."""
    root = Path(index_dir).resolve()
    generation = manifest.get('storage_directory')
    if generation is None:
        return root
    path = (root / generation).resolve()
    if not path.is_relative_to(root / 'generations') or path == root / 'generations':
        raise ValueError('Invalid index generation path in manifest')
    return path


def open_chroma(index_dir: str | Path, *, create: bool = False, manifest: dict | None = None):
    try:
        import chromadb
        from chromadb.config import Settings
    except ImportError as exc:
        raise RuntimeError('ChromaDB is not installed. Run pip install -r requirements.txt.') from exc
    path = Path(index_dir)
    if not create:
        if manifest is None:
            manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
        path = storage_path(path, manifest)
    client = chromadb.PersistentClient(path=str(path / 'chroma'),
                                      settings=Settings(anonymized_telemetry=False))
    if create:
        return client.create_collection(name=COLLECTION, embedding_function=None,
                                        metadata={'hnsw:space': 'cosine'})
    return client.get_collection(name=COLLECTION, embedding_function=None)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open('w', encoding='utf-8') as out:
        for item in rows:
            out.write(json.dumps(item, ensure_ascii=False, separators=(',', ':')) + '\n')


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding='utf-8') as inp:
        return [json.loads(line) for line in inp if line.strip()]


def build_index(index_dir: str | Path, records: list[dict], capas: dict[str, list[dict]],
                source_stats: dict, *, model_name: str = DEFAULT_MODEL,
                mock_embeddings: bool = False, chunk_size: int = CHUNK_SIZE,
                chunk_overlap: int = CHUNK_OVERLAP, batch_size: int = 16,
                replace: bool = False, embedder=None, runtime: str = 'torch') -> dict:
    path = Path(index_dir).resolve()
    if batch_size <= 0:
        raise ValueError('batch_size must be positive')
    # Validate before writing anything. Never recursively delete a caller's path.
    split_chunks('', chunk_size, chunk_overlap)
    if path.exists() and any(path.iterdir()):
        if not replace:
            raise FileExistsError(f'Index {path} already exists. Pass --replace to REBUILD it.')
        if not (path / 'manifest.json').is_file():
            # Permit retrying a failed first build, but refuse unrelated folders.
            if {item.name for item in path.iterdir()} != {'generations'}:
                raise ValueError('Refusing to replace a non-MATCH directory; choose a new --index-dir')
        else:
            load_index_metadata(path)
    chunks = []
    if len({r['pr_id'] for r in records}) != len(records):
        raise ValueError('Deviation IDs must be unique')
    for record in records:
        fields = record.get('indexed_field_texts') or {'COMBINED_TEXT': record['text']}
        seq = 0
        for field, text in fields.items():
            for fragment in split_chunks(text, chunk_size, chunk_overlap):
                chunks.append({
                    'chunk_id': f"{record['pr_id']}_chunk_{seq}",
                    'pr_id': record['pr_id'],
                    'site': record['site'],
                    'division': record['division'],
                    'source_field': field,
                    'doc': fragment,
                })
                seq += 1
    if not chunks:
        raise ValueError('No nonempty chunks to index')
    embedder = embedder or (MockEmbedder() if mock_embeddings else make_mpnet_embedder(model_name, batch_size, runtime))
    if mock_embeddings and not isinstance(embedder, MockEmbedder):
        raise ValueError('Cannot mark a real embedding model as mock')
    if embedder.model_name != ('MOCK-hashed-token-embeddings-NOT-MPNET' if mock_embeddings else model_name):
        raise ValueError(f'Embedding model mismatch: {embedder.model_name} vs configured {model_name}')

    # Reuse vectors only for byte-identical normalized text with the exact same
    # model/runtime/dimension contract. Changed scientific passages are encoded
    # again; the active generation is never edited in place.
    vector_cache = {}
    if replace and (path / 'manifest.json').is_file():
        previous, _, prior_chunks, _ = load_index_metadata(path)
        expected_backend = 'MOCK_UNFIT_FOR_REAL_SEARCH' if mock_embeddings else 'MPNet'
        if (previous.get('model_name') == embedder.model_name and previous.get('embedding_backend') == expected_backend
                and previous.get('embedding_runtime', 'torch') == ('mock' if mock_embeddings else getattr(embedder, 'runtime', runtime))
                and previous.get('embedding_dimensions') == 768 and previous.get('normalize_embeddings') is True):
            prior_collection = open_chroma(path, manifest=previous)
            needed = {chunk['doc'] for chunk in chunks}
            unique = {}
            for chunk in prior_chunks:
                if chunk['doc'] in needed:
                    unique.setdefault(chunk['doc'], chunk['chunk_id'])
            items = list(unique.items())
            for start in range(0, len(items), 400):
                values = items[start:start+400]
                stored = prior_collection.get(ids=[key for _,key in values], include=['embeddings'])
                vectors = stored.get('embeddings')
                if vectors is None or len(vectors) != len(values):
                    # Reuse is an optimization; a new generation can always be
                    # encoded independently if the old store cannot supply it.
                    vector_cache.clear()
                    break
                by_id = dict(zip(stored['ids'], unit_rows(vectors)))
                vector_cache.update((text, by_id[key]) for text,key in values)
    reused = len(vector_cache)
    print(f'Reusing {reused} unchanged text vectors from the previous generation.',flush=True)

    # Build beside the active generation. Commit a small manifest only after all
    # vectors persist, so a failed model download/rebuild keeps the old index usable.
    generation = Path('generations') / uuid4().hex
    output = path / generation
    output.mkdir(parents=True)
    write_jsonl(output / 'records.jsonl', records)
    write_jsonl(output / 'chunks.jsonl', chunks)
    (output / 'capas.json').write_text(json.dumps(capas, ensure_ascii=False), encoding='utf-8')
    collection = open_chroma(output, create=True)
    # Group similar lengths to reduce padding and encode repeated text once.
    build_chunks = sorted(chunks, key=lambda chunk: (len(chunk['doc']), chunk['chunk_id']))
    started = last_progress = time.monotonic()
    # Model.encode applies its own small compute batches. Persistence uses
    # larger transactions to avoid thousands of small Chroma/SQLite commits.
    persist_batch_size=max(256,batch_size)
    for start in range(0, len(build_chunks), persist_batch_size):
        batch = build_chunks[start:start + persist_batch_size]
        missing = list(dict.fromkeys(chunk['doc'] for chunk in batch if chunk['doc'] not in vector_cache))
        if missing:
            encoded = unit_rows(embedder.encode(missing))
            if encoded.shape != (len(missing), 768):
                raise ValueError('Expected one 768D MPNet vector per unique chunk')
            vector_cache.update(zip(missing, encoded))
        vectors = np.asarray([vector_cache[chunk['doc']] for chunk in batch])
        if vectors.shape != (len(batch), 768):
            raise ValueError(f'Expected {len(batch)} x 768 MPNet vectors; received {vectors.shape}')
        vectors = unit_rows(vectors)
        collection.upsert(
            ids=[c['chunk_id'] for c in batch],
            documents=[c['doc'] for c in batch],
            embeddings=vectors.tolist(),
            metadatas=[{key: c[key] for key in ('pr_id', 'site', 'division', 'source_field')} for c in batch],
        )
        if start == 0 or time.monotonic() - last_progress >= 30 or start + len(batch) == len(chunks):
            elapsed = time.monotonic() - started
            print(f'Indexed {start + len(batch)}/{len(chunks)} chunks; {elapsed:.0f}s elapsed...', flush=True)
            last_progress = time.monotonic()
    stored = int(collection.count())
    if stored != len(chunks):
        raise RuntimeError(f'Chroma count mismatch: expected {len(chunks)}, observed {stored}')
    manifest = {
        'version': 'MATCH-local-v1',
        'ready': True,
        'storage_directory': generation.as_posix(),
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'model_name': embedder.model_name,
        'embedding_backend': 'MOCK_UNFIT_FOR_REAL_SEARCH' if mock_embeddings else 'MPNet',
        'embedding_runtime': 'mock' if mock_embeddings else getattr(embedder, 'runtime', runtime),
        'unique_embedded_texts': len(vector_cache),
        'reused_embedded_texts': reused,
        'new_embedded_texts': len(vector_cache) - reused,
        'embedding_dimensions': 768,
        'normalize_embeddings': True,
        'vector_db': 'ChromaDB',
        'distance': 'cosine',
        'collection': COLLECTION,
        'chunk_size': chunk_size,
        'chunk_overlap': chunk_overlap,
        'chunking': 'fixed-character-windows-v1',
        'field_chunking': 'separate-fields; no chunk crosses a field boundary',
        'text_fields': source_stats.get('indexed_fields', []),
        'candidate_strategy': 'semantic + full authorized-site BM25 union',
        'ranking': '0.70*clipped_cosine+0.20*normalized_positive_BM25+0.10*normal_Jaccard',
        'record_count': len(records),
        'chunk_count': len(chunks),
        'site_counts': dict(sorted(Counter(r['site'] for r in records).items())),
        'capa_link_count': sum(map(len, capas.values())),
        'source_statistics': source_stats,
    }
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
    pending = path / f'manifest-{uuid4().hex}.tmp'
    pending.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
    pending.replace(path / 'manifest.json')
    return manifest


def load_index_metadata(index_dir: str | Path) -> tuple[dict, dict[str, dict], list[dict], dict[str, list[dict]]]:
    path = Path(index_dir)
    manifest_path = path / 'manifest.json'
    if not manifest_path.exists():
        raise FileNotFoundError(f'Index is missing/not complete: {manifest_path}. Run the build command.')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('ready') is not True:
        raise ValueError('Index is not ready')
    output = storage_path(path, manifest)
    records = {r['pr_id']: r for r in read_jsonl(output / 'records.jsonl')}
    chunks = read_jsonl(output / 'chunks.jsonl')
    capas = json.loads((output / 'capas.json').read_text(encoding='utf-8'))
    if len(records) != manifest.get('record_count') or len(chunks) != manifest.get('chunk_count'):
        raise ValueError('Metadata does not match index manifest. Rebuild the index.')
    return manifest, records, chunks, capas
