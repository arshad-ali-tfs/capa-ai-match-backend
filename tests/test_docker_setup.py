"""Offline preparation, retry, integrity, site scope and readiness checks."""
import csv
import json
import io
import tarfile
from pathlib import Path

import pytest

from capa_match import build, docker_setup as setup
from match_v1 import core, index
from match_v1.testing import MemoryCollection


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'original.csv'
    with path.open('w', newline='', encoding='utf8') as stream:
        writer = csv.DictWriter(stream, fieldnames=['PR_ID', 'SITE', 'PSG_Division', 'RECORD_TYPE', 'STATUS', 'DETAILED_DESCRIPTION', 'CONCLUSION'])
        writer.writeheader()
        for identifier, site in [('001', 'Site A'), ('002', 'Site B')]:
            writer.writerow(dict(PR_ID=identifier, SITE=site, PSG_Division='DPD', RECORD_TYPE='Deviation',
                                 STATUS='Closed - Done', DETAILED_DESCRIPTION='FICTIONAL: lot AB-42, 20 mL on 12-May-2025.\nSecond line.',
                                 CONCLUSION='FICTIONAL: instrument verified.'))
    return path


def test_source_import_preserves_exact_csv_and_reuses_custom_source(tmp_path, monkeypatch, source):
    data = tmp_path / 'data'
    monkeypatch.delenv('CAPA_SOURCE_SNAPSHOT', raising=False)
    first = setup.prepare_source(data, source)
    assert Path(first['CAPA_SOURCE_CSV']).read_bytes() == source.read_bytes()
    assert first['accepted_rows'] == 2
    assert setup.prepare_source(data) == first
    Path(first['CAPA_SOURCE_CSV']).write_text('changed')
    with pytest.raises(ValueError, match='source CSV has changed'):
        setup.prepare_source(data)


def test_versioned_files_and_cache_are_verified_and_reusable(tmp_path_factory, monkeypatch):
    def no_sendfile(*args, **kwargs):
        raise AssertionError('Large Docker bind-mount copies must use buffered I/O')
    monkeypatch.setattr(setup.shutil, 'copyfile', no_sendfile)
    tmp_path = tmp_path_factory.mktemp('cache')
    root, data, bundle = [tmp_path / name for name in ('root', 'data', 'bundle')]
    root.mkdir(); bundle.mkdir()
    model = bundle / 'runtime/mpnet/config.json'
    model.parent.mkdir(parents=True)
    model.write_text('{"fixture":true}')
    manifest = dict(embedding_model=core.DEFAULT_MODEL, embedding_revision='f' * 40, model_directory='data/runtime/mpnet',
                    files=[dict(path='data/runtime/mpnet/config.json', size_bytes=model.stat().st_size, sha256=setup.digest(model))])
    (root / 'runtime-assets.json').write_text(json.dumps(manifest))
    assert setup.prepare_files(root, data, bundle) == manifest
    cache = data / 'models/huggingface/models--sentence-transformers--all-mpnet-base-v2'
    assert (cache / 'refs/main').read_text() == 'f' * 40
    assert (cache / 'snapshots' / ('f' * 40) / 'config.json').read_bytes() == model.read_bytes()
    setup.prepare_files(root, data, bundle)
    (data / 'runtime/mpnet/config.json').write_text('damaged')
    with pytest.raises(ValueError, match='damaged or changed'):
        setup.prepare_files(root, data, bundle)


def test_runtime_paths_cannot_escape_storage(tmp_path):
    for path in ('../outside', '/tmp/outside', '.'):
        with pytest.raises(ValueError, match='inside'):
            setup.contained(tmp_path, path)


def test_cross_mount_copy_uses_buffered_fallback(tmp_path, monkeypatch):
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.write_bytes(b'fixture weights')
    def cross_device(*args): raise OSError('cross-device hard links unavailable')
    def no_sendfile(*args, **kwargs): raise AssertionError('must use buffered copy')
    monkeypatch.setattr(setup.os, 'link', cross_device)
    monkeypatch.setattr(setup.shutil, 'copyfile', no_sendfile)
    setup.verified_copy(source, target, source.stat().st_size, setup.digest(source))
    assert target.read_bytes() == source.read_bytes()


@pytest.mark.parametrize('setting,expected', [('auto', ['Site A', 'Site B']), ('["Site B"]', ['Site B']),
                                             ('["Site A","Site A"]', ['Site A'])])
def test_sites_are_explicit_and_never_widen_a_configured_restriction(setting, expected):
    assert setup.allowed_sites(setting, ['Site A', 'Site B']) == expected


@pytest.mark.parametrize('setting', ['[]', 'null', '{}', '[1]', '["Unknown"]', 'broken'])
def test_invalid_or_empty_site_configuration_fails_clearly(setting):
    with pytest.raises(ValueError):
        setup.allowed_sites(setting, ['Site A'])


@pytest.fixture
def builder(tmp_path, monkeypatch, source):
    monkeypatch.delenv('CAPA_SOURCE_SNAPSHOT', raising=False)
    monkeypatch.setenv('MATCH_MODEL_NAME', core.DEFAULT_MODEL)
    monkeypatch.setenv('MATCH_ALLOWED_SITES_JSON', 'auto')
    monkeypatch.setattr(build, 'ROOT', tmp_path)
    data = tmp_path / 'data'
    prepared = setup.prepare_source(data, source)
    collections, calls = [], []
    def collection(*args, create=False, **kwargs):
        if create: collections.append(MemoryCollection())
        return collections[-1]
    monkeypatch.setattr(index, 'open_chroma', collection)
    class OfflineModel(core.MockEmbedder):
        model_name = core.DEFAULT_MODEL
        runtime = 'onnx-fp32'
        def encode(self, texts):
            calls.append(texts)
            return super().encode(texts)
    monkeypatch.setattr(core, 'make_mpnet_embedder', lambda *args, **kwargs: OfflineModel())
    assets = dict(embedding_model=core.DEFAULT_MODEL, embedding_revision='f' * 40)
    def run(limit=0, rebuild=False, seed=None):
        configuration = {**assets, **({'bundled_index': seed} if seed else {})}
        return setup.prepare_index(tmp_path, data, configuration, prepared, limit, 16, rebuild)
    return run, calls, collections, tmp_path


def test_completed_index_is_reused_and_pilot_is_separate_from_full_scope(builder):
    run, calls, collections, _ = builder
    first = run()
    count = len(calls)
    assert first['records'] == 2 and first['sites'] == 2
    assert run() == first
    assert len(calls) == count and len(collections) == 1
    pilot = run(limit=1)
    assert pilot['records'] == 1 and pilot['MATCH_INDEX_DIR'] != first['MATCH_INDEX_DIR']
    assert Path(first['MATCH_INDEX_DIR'], 'manifest.json').is_file()


def test_rebuild_publishes_new_generation_and_retains_previous_files(builder):
    run, _, _, _ = builder
    first = run()
    second = run(rebuild=True)
    assert first['MATCH_INDEX_DIR'] == second['MATCH_INDEX_DIR']
    assert first['generation'] != second['generation']
    assert Path(first['MATCH_INDEX_DIR'], first['generation'], 'records.jsonl').is_file()


def test_interrupted_first_build_retries_without_deleting_its_files(builder, monkeypatch):
    run, _, _, root = builder
    original = core.make_mpnet_embedder
    class Failed(core.MockEmbedder):
        model_name = core.DEFAULT_MODEL
        def encode(self, texts): raise RuntimeError('fictional interruption')
    monkeypatch.setattr(core, 'make_mpnet_embedder', lambda *args, **kwargs: Failed())
    with pytest.raises(RuntimeError, match='interruption'): run()
    partial = list((root / 'data/match-managed').glob('*/generations/*'))
    assert len(partial) == 1
    monkeypatch.setattr(core, 'make_mpnet_embedder', original)
    assert run()['records'] == 2
    assert partial[0].is_dir()


def test_incompatible_completed_index_is_not_silently_served(builder):
    run, _, _, _ = builder
    first = run()
    path = Path(first['MATCH_INDEX_DIR']) / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['embedding_backend'] = 'MOCK_UNFIT_FOR_REAL_SEARCH'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='policy'): run()


def test_verified_index_can_be_seeded_without_reembedding(builder):
    run, calls, _, root = builder
    built = run()
    index_root = Path(built['MATCH_INDEX_DIR'])
    archive = root / 'index.tar.gz'
    with tarfile.open(archive, 'w:gz') as stream:
        stream.add(index_root / 'manifest.json', arcname='manifest.json')
        stream.add(index_root / built['generation'], arcname=built['generation'])
    count = len(calls)
    target = root / 'seeded'
    setup.seed_index(archive, target)
    manifest, records, _, _ = index.load_index_metadata(target)
    assert len(records) == 2 and manifest['embedding_backend'] == 'MPNet'
    assert len(calls) == count


@pytest.mark.parametrize('kind', ['escape', 'link'])
def test_index_archive_cannot_write_outside_its_staging_directory(tmp_path, kind):
    archive = tmp_path / 'invalid.tar.gz'
    with tarfile.open(archive, 'w:gz') as stream:
        member = tarfile.TarInfo('../outside' if kind == 'escape' else 'linked')
        if kind == 'link':
            member.type = tarfile.SYMTYPE
            member.linkname = str(tmp_path / 'outside')
            stream.addfile(member)
        else:
            member.size = 3
            stream.addfile(member, io.BytesIO(b'bad'))
    with pytest.raises(ValueError): setup.seed_index(archive, tmp_path / 'target')
    assert not (tmp_path / 'outside').exists()
    assert not list(tmp_path.glob('seed-*'))


@pytest.mark.parametrize('compatible,narrow_scope', [(True,False),(False,False),(True,True)])
def test_bundled_index_requires_a_validated_source_binding(builder, compatible, narrow_scope, monkeypatch):
    run, calls, _, root = builder
    first = run()
    index_root = Path(first['MATCH_INDEX_DIR'])
    archive = root / 'data/index.tar.gz'
    with tarfile.open(archive, 'w:gz') as stream:
        stream.add(index_root / 'manifest.json', arcname='manifest.json')
        stream.add(index_root / first['generation'], arcname=first['generation'])
    seed = dict(path='data/index.tar.gz', contract=setup.index_contract(root),
                policy=dict(snapshot='legacy-workbook', model=core.DEFAULT_MODEL, revision='f' * 40,
                            runtime='onnx-fp32', division='DPD' if narrow_scope else '', status_scope='completed' if narrow_scope else 'all', limit=2, chunk_size=500, chunk_overlap=50,
                            text_fields=['DETAILED_DESCRIPTION', 'CONCLUSION']),
                equivalent_snapshot_ids=[first['snapshot_id']] if compatible else ['another-source'])
    count = len(calls)
    prepared = run(limit=2, seed=seed)
    assert prepared['snapshot_id'] == first['snapshot_id']
    assert (len(calls) == count) is (compatible and not narrow_scope)
    assert index.load_index_metadata(Path(prepared['MATCH_INDEX_DIR']))[0]['source_snapshot_id'] == first['snapshot_id']


def test_verify_checks_real_model_and_both_search_scopes(monkeypatch):
    calls = []
    def api(base, path, body=None):
        calls.append((path, body))
        if path == '/index': return dict(ready=True, mock_embeddings=False, source_snapshot_id='s', records=2, chunks=2)
        if path == '/source': return dict(snapshot_id='s', accepted_rows=3)
        if path == '/sites': return dict(sites=[dict(site='Site A'), dict(site='Site B')])
        return dict(results=[dict(site='Site A')], searched_sites=['Site A', 'Site B'])
    monkeypatch.setattr(setup, 'api', api)
    assert setup.verify('http://fixture')['real_embeddings']
    assert len([body for path, body in calls if path == '/search']) == 2
