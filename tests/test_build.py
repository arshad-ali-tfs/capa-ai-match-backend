"""Offline command contracts for the snapshot-aware MATCH builder."""
import csv
import importlib.util
import json
import os
from pathlib import Path
from dotenv import dotenv_values, set_key
import pytest
from capa_match.infrastructure.data.export import export_csv
from capa_match.infrastructure.data.importer import import_snapshot
from capa_match.infrastructure.data.repository import SqliteRecordRepository
from match_v1 import core, index
from match_v1.testing import MemoryCollection


@pytest.fixture
def wrapper(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location('platform_match_build_test', Path(__file__).parents[1] / 'src/capa_match/build.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    path = tmp_path / 'fictional.csv'
    with path.open('w', newline='', encoding='utf8') as stream:
        writer = csv.DictWriter(stream, fieldnames=['PR_ID', 'SITE', 'PSG_Division', 'RECORD_TYPE', 'STATUS', 'DETAILED_DESCRIPTION', 'CONCLUSION', 'INVESTIGATION'])
        writer.writeheader()
        for ident, site, division in [('101', 'Site A', 'DPD'), ('102', 'Site A', 'DPD'), ('103', 'Site B', 'DPD'), ('104', 'Site A', 'DSD')]:
            writer.writerow({'PR_ID': ident, 'SITE': site, 'PSG_Division': division,
                             'RECORD_TYPE': 'Deviation', 'STATUS': 'Closed - Done',
                             'DETAILED_DESCRIPTION': f'FICTIONAL TEST: bracket event {ident}.',
                             'CONCLUSION': 'FICTIONAL TEST: bracket replaced.',
                             'INVESTIGATION': 'FICTIONAL TEST: verification identified the bracket.'})
    snapshot = import_snapshot(path, tmp_path / 'snapshots')
    repository = SqliteRecordRepository(snapshot)
    exported = tmp_path / 'snapshot.csv'
    export_csv(repository, exported)
    config = {'CAPA_SOURCE_SNAPSHOT': str(snapshot), 'CAPA_SOURCE_CSV': str(exported), 'CAPA_DATA_DIR': str(tmp_path / 'data')}
    for key in [*config, 'MATCH_INDEX_DIR', 'MATCH_ALLOWED_SITES_JSON', 'MATCH_MODEL_NAME']:
        monkeypatch.setenv(key, os.environ.get(key, ''))
    (tmp_path / '.env').write_text('\n'.join(key + '=' + json.dumps(value) for key, value in config.items()) + '\n', encoding='utf8')
    collections = []
    def memory_collection(*args, **kwargs):
        collection = MemoryCollection()
        collections.append(collection)
        return collection
    monkeypatch.setattr(index, 'open_chroma', memory_collection)
    def forbidden_model(*args, **kwargs):
        raise AssertionError('Offline test attempted real model loading')
    monkeypatch.setattr(core, 'make_mpnet_embedder', forbidden_model)
    return module, repository.metadata(), exported, collections


def test_original_flags_drive_real_ingestion_chunking_and_snapshot_activation(wrapper, monkeypatch):
    module, metadata, source, collections = wrapper
    requested = []
    class OfflineModel(core.MockEmbedder):
        model_name = 'fictional-checkpoint'
        runtime = 'torch'
    def model(name, batch_size, *, runtime):
        requested.append((name, batch_size, runtime))
        return OfflineModel()
    monkeypatch.setattr(core, 'make_mpnet_embedder', model)
    target = module.ROOT / 'custom-index'
    module.main(['--gtw', str(source), '--index-dir', str(target), '--division', 'DSD', '--site', 'Site A',
                 '--limit', '1', '--model', 'fictional-checkpoint', '--batch-size', '2', '--runtime', 'torch',
                 '--chunk-size', '60', '--chunk-overlap', '10', '--text-field', 'INVESTIGATION', '--text-field', 'CONCLUSION', '--replace'])
    assert requested == [('fictional-checkpoint', 2, 'torch')]
    manifest, records, chunks, _ = index.load_index_metadata(target)
    assert list(records) == ['104']
    assert manifest['chunk_size'] == 60 and manifest['chunk_overlap'] == 10
    assert manifest['text_fields'] == ['INVESTIGATION', 'CONCLUSION']
    assert manifest['embedding_runtime'] == 'torch'
    assert manifest['source_snapshot_id'] == metadata['snapshot_id']
    assert manifest['source_statistics']['division_filter'] == 'DSD'
    assert manifest['source_statistics']['site_filter'] == 'Site A'
    assert collections[0].count() == len(chunks)
    generation = index.storage_path(target, manifest)
    assert json.loads((generation / 'manifest.json').read_text())['source_snapshot_id'] == metadata['snapshot_id']
    config = dotenv_values(module.ROOT / '.env')
    assert Path(config['MATCH_INDEX_DIR']) == target
    assert config['MATCH_MODEL_NAME'] == 'fictional-checkpoint'
    assert json.loads(config['MATCH_ALLOWED_SITES_JSON']) == ['Site A']


def test_mock_build_is_offline_and_keeps_platform_defaults(wrapper, capsys):
    module, metadata, _, _ = wrapper
    module.main(['--mock-embeddings'])
    target = Path(dotenv_values(module.ROOT / '.env')['MATCH_INDEX_DIR'])
    manifest, records, _, _ = index.load_index_metadata(target)
    assert len(records) == 4
    assert manifest['text_fields'] == ['DETAILED_DESCRIPTION', 'CONCLUSION']
    assert manifest['embedding_backend'] == 'MOCK_UNFIT_FOR_REAL_SEARCH'
    assert manifest['embedding_runtime'] == 'mock'
    assert manifest['source_snapshot_id'] == metadata['snapshot_id']
    assert 'OFFLINE DEMO' in capsys.readouterr().out
    generation = manifest['storage_directory']
    module.main([])
    assert json.loads((target / 'manifest.json').read_text())['storage_directory'] == generation


def test_default_real_model_runtime_and_batch_are_preserved(wrapper, monkeypatch):
    module, _, _, _ = wrapper
    requested = []
    class OfflineModel(core.MockEmbedder):
        model_name = core.DEFAULT_MODEL
        runtime = 'onnx-fp32'
    def model(name, batch_size, *, runtime):
        requested.append((name, batch_size, runtime))
        return OfflineModel()
    monkeypatch.setattr(core, 'make_mpnet_embedder', model)
    module.main([])
    assert requested == [(core.DEFAULT_MODEL, 16, 'onnx-fp32')]


def test_existing_index_requires_explicit_rebuild_for_changed_settings(wrapper):
    module, _, _, _ = wrapper
    module.main(['--mock-embeddings'])
    target = Path(dotenv_values(module.ROOT / '.env')['MATCH_INDEX_DIR'])
    initial = json.loads((target / 'manifest.json').read_text())
    with pytest.raises(SystemExit):
        module.main(['--mock-embeddings', '--site', 'Site A'])
    assert json.loads((target / 'manifest.json').read_text()) == initial
    module.main(['--rebuild', '--mock-embeddings', '--site', 'Site A', '--limit', '1'])
    manifest, records, _, _ = index.load_index_metadata(target)
    assert len(records) == 1 and manifest['storage_directory'] != initial['storage_directory']
    assert (target / initial['storage_directory'] / 'manifest.json').is_file()


def test_builder_refuses_to_stamp_unrelated_source_or_reuse_foreign_snapshot(wrapper):
    module, _, _, _ = wrapper
    unrelated = module.ROOT / 'unrelated.csv'
    unrelated.write_text('unrelated source', encoding='utf8')
    with pytest.raises(SystemExit):
        module.main(['--gtw', str(unrelated), '--mock-embeddings'])
    module.main(['--mock-embeddings'])
    target = Path(dotenv_values(module.ROOT / '.env')['MATCH_INDEX_DIR'])
    manifest = json.loads((target / 'manifest.json').read_text())
    manifest['source_snapshot_id'] = 'another-source'
    (target / 'manifest.json').write_text(json.dumps(manifest), encoding='utf8')
    with pytest.raises(RuntimeError, match='different source version'):
        module.main([])


@pytest.mark.parametrize('flags', [['--limit', '-1'], ['--batch-size', '0'], ['--chunk-size', '49'], ['--chunk-overlap', '500']])
def test_invalid_build_options_fail_before_models_or_index_writes(wrapper, flags):
    module, _, _, collections = wrapper
    with pytest.raises(SystemExit):
        module.main(flags)
    assert not collections


def test_ambiguous_source_normalization_is_rejected_before_replacing_index(wrapper, capsys):
    module, _, _, collections = wrapper
    module.main(['--mock-embeddings'])
    target = Path(dotenv_values(module.ROOT / '.env')['MATCH_INDEX_DIR'])
    before = (target / 'manifest.json').read_bytes()
    source = module.ROOT / 'fictional.csv'
    with source.open('a', newline='', encoding='utf8') as stream:
        csv.writer(stream).writerow(['000101', 'Site A', 'DPD', 'Deviation', 'Closed - Done',
                                    'FICTIONAL TEST: conflicting source ID.', '', ''])
    snapshot = import_snapshot(source, module.ROOT / 'snapshots')
    exported = module.ROOT / 'conflicting-snapshot.csv'
    export_csv(SqliteRecordRepository(snapshot), exported)
    set_key(str(module.ROOT / '.env'), 'CAPA_SOURCE_SNAPSHOT', str(snapshot))
    set_key(str(module.ROOT / '.env'), 'CAPA_SOURCE_CSV', str(exported))
    with pytest.raises(SystemExit):
        module.main(['--rebuild', '--mock-embeddings', '--index-dir', str(target)])
    assert 'Ambiguous MATCH identifier normalization' in capsys.readouterr().err
    assert (target / 'manifest.json').read_bytes() == before
    assert len(collections) == 1
