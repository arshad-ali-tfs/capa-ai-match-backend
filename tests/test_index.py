import json
from pathlib import Path

import match_v1.index as index
import pytest
from match_v1.core import MockEmbedder
from match_v1.ingest import load_gtw
from match_v1.testing import MemoryCollection

DEMO = Path(__file__).parent / 'fixtures' / 'demo_gtw.csv'


def test_persisted_manifest_and_source_metadata(monkeypatch, tmp_path):
    collection = MemoryCollection()
    monkeypatch.setattr(index, 'open_chroma', lambda index_dir, create=False: collection)
    records, capas, stats = load_gtw(DEMO, division='DPD', limit=0)
    manifest = index.build_index(tmp_path / 'build', records, capas, stats,
                                 mock_embeddings=True, batch_size=3)
    loaded, stored_records, chunks, capa_links = index.load_index_metadata(tmp_path / 'build')
    assert manifest['chunk_count'] == collection.count()
    assert manifest['record_count'] == len(records)
    assert loaded['model_name'] == MockEmbedder.model_name
    assert len(stored_records) == 8
    assert capa_links['101'][0]['pr_id'] == '901'
    assert len(chunks) >= 8


def test_failed_rebuild_preserves_previous_index(monkeypatch, tmp_path):
    monkeypatch.setattr(index, 'open_chroma', lambda *args, **kwargs: MemoryCollection())
    records, capas, stats = load_gtw(DEMO, limit=0)
    path = tmp_path / 'index'
    original = index.build_index(path, records, capas, stats, mock_embeddings=True)

    class BrokenEmbedder(MockEmbedder):
        def encode(self, texts):
            raise RuntimeError('Model unavailable')

    with pytest.raises(RuntimeError, match='Model unavailable'):
        index.build_index(path, records, capas, stats, mock_embeddings=True,
                          replace=True, embedder=BrokenEmbedder())
    assert index.load_index_metadata(path)[0] == original
    assert (path / original['storage_directory'] / 'records.jsonl').is_file()
    rebuilt = index.build_index(path, records[:1], capas, stats, mock_embeddings=True, replace=True)
    assert rebuilt['storage_directory'] != original['storage_directory']
    assert index.load_index_metadata(path)[0]['record_count'] == 1


def test_replace_refuses_unrelated_directory(tmp_path):
    (tmp_path / 'important.txt').write_text('Keep this file')
    records, capas, stats = load_gtw(DEMO, limit=1)
    with pytest.raises(ValueError, match='non-MATCH directory'):
        index.build_index(tmp_path, records, capas, stats, mock_embeddings=True, replace=True)
    assert (tmp_path / 'important.txt').read_text() == 'Keep this file'


def test_manifest_cannot_escape_index_directory(tmp_path):
    with pytest.raises(ValueError, match='generation path'):
        index.storage_path(tmp_path, {'storage_directory': '../elsewhere'})


def test_fields_are_searchable_without_crossing_boundaries(monkeypatch, tmp_path):
    monkeypatch.setattr(index, 'open_chroma', lambda *args, **kwargs: MemoryCollection())
    records, capas, stats = load_gtw(DEMO, limit=1)
    record = records[0]
    record['indexed_field_texts']['CONCLUSION'] = 'Root cause was a damaged instrument connection.'
    stats['indexed_fields'] = ['DETAILED_DESCRIPTION', 'CONCLUSION']
    index.build_index(tmp_path / 'fields', records, capas, stats, mock_embeddings=True)
    _, _, chunks, _ = index.load_index_metadata(tmp_path / 'fields')
    assert {chunk['source_field'] for chunk in chunks} == {'DETAILED_DESCRIPTION', 'CONCLUSION'}
    for chunk in chunks:
        assert chunk['doc'] in record['indexed_field_texts'][chunk['source_field']]


@pytest.mark.parametrize('options', [{'batch_size': 0}, {'chunk_size': 50, 'chunk_overlap': 50}])
def test_invalid_build_parameters_do_not_write(tmp_path, options):
    path = tmp_path / 'index'
    records, capas, stats = load_gtw(DEMO, limit=1)
    with pytest.raises(ValueError):
        index.build_index(path, records, capas, stats, mock_embeddings=True, **options)
    assert not path.exists()
