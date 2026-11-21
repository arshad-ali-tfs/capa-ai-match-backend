"""Prepare a real, persistent MATCH installation inside its Linux container."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import tarfile
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from filelock import FileLock


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding='utf8')) if path.is_file() else {}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(uuid4().hex[:12] + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n', encoding='utf8')
    temporary.replace(path)


def contained(base, relative):
    target = (base / relative).resolve()
    if not target.is_relative_to(base.resolve()) or target == base.resolve():
        raise ValueError('Runtime path must stay inside its storage directory')
    return target


def verified_copy(source, target, size, checksum):
    if target.is_file():
        if target.stat().st_size == size and digest(target) == checksum:
            return
        raise ValueError(f'Runtime file is damaged or changed: {target}. Restore the versioned file before retrying.')
    if not source.is_file() or source.stat().st_size != size or digest(source) != checksum:
        raise ValueError(f'Missing or damaged repository runtime file: {source}. Check Git LFS access and rerun.')
    target.parent.mkdir(parents=True, exist_ok=True)
    # Keep atomic sibling names short under deep Hugging Face cache paths.
    temporary = target.with_name(uuid4().hex[:12] + '.tmp')
    try:
        try:
            os.link(source, temporary)  # Immutable cache files can share storage.
        except OSError:
            with source.open('rb') as incoming, temporary.open('wb') as outgoing:
                shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        if digest(temporary) != checksum:
            raise ValueError('Runtime file changed while copying')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_files(root, data, bundle):
    manifest = read_json(root / 'runtime-assets.json')
    for item in manifest['files']:
        path = Path(item['path'])
        if path.parts[0] != 'data':
            raise ValueError('Runtime manifest paths must start with data/')
        relative = path.relative_to('data')
        if item['path'] == manifest.get('bundled_index', {}).get('path'):
            archive = contained(bundle, relative)
            if not archive.is_file() or digest(archive) != item['sha256']:
                raise ValueError('The bundled Linux index is missing or damaged. Check Git LFS and rerun.')
            continue  # Used during preparation only; do not store a second archive.
        verified_copy(contained(bundle, relative), contained(data, relative), item['size_bytes'], item['sha256'])
    print(f"Verified {len(manifest['files'])} source/model files.", flush=True)
    # Build the standard HF cache inside Docker, avoiding long Git checkout paths
    # and any model downloads. Keep the existing checkpoint name and runtime.
    cache = data / 'models/huggingface'
    model = contained(cache, 'models--' + manifest['embedding_model'].replace('/', '--'))
    model_dir = Path(manifest['model_directory'])
    for item in manifest['files']:
        path = Path(item['path'])
        if path.is_relative_to(model_dir):
            relative = path.relative_to(model_dir)
            verified_copy(contained(data, path.relative_to('data')),
                          contained(model / 'snapshots' / manifest['embedding_revision'], relative),
                          item['size_bytes'], item['sha256'])
    reference = model / 'refs/main'
    reference.parent.mkdir(parents=True, exist_ok=True)
    if reference.is_file() and reference.read_text().strip() != manifest['embedding_revision']:
        raise ValueError('The MPNet cache points to another revision. Use a separate data directory.')
    reference.write_text(manifest['embedding_revision'], encoding='utf8')
    return manifest


def prepare_source(data, source=None, sheet=None):
    from .infrastructure.data.importer import import_snapshot
    from .infrastructure.data.repository import SqliteRecordRepository
    from .infrastructure.data.export import export_csv

    prior = read_json(data / '.setup/source.json')
    configured = os.getenv('CAPA_SOURCE_SNAPSHOT', '').strip()
    input_source = Path(source) if source else None
    if input_source:
        snapshot = import_snapshot(input_source, data / 'sources', sheet=sheet)
    elif configured and 'REPLACE_WITH_' not in configured:
        snapshot = Path(configured).resolve(strict=True)
    elif prior:
        snapshot = Path(prior['CAPA_SOURCE_SNAPSHOT']).resolve(strict=True)
    else:
        input_source = data / 'sources/trackwise_records.csv'
        snapshot = import_snapshot(input_source, data / 'sources')
    if not snapshot.is_relative_to(data.resolve()):
        raise ValueError('Source snapshot must be inside the mounted data directory.')
    repository = SqliteRecordRepository(snapshot)
    metadata = repository.metadata()
    source_csv = snapshot.with_suffix('.csv')
    if (prior.get('snapshot_id') == metadata['snapshot_id'] and prior.get('source_csv_sha256')
            and source_csv.is_file() and digest(source_csv) != prior['source_csv_sha256']):
        raise ValueError('The prepared source CSV has changed. Restore it or import a new source.')
    if not source_csv.exists():
        temporary = source_csv.with_suffix('.partial.csv')
        if input_source and input_source.suffix.lower() == '.csv' and metadata['quarantined_rows'] == 0:
            temporary.unlink(missing_ok=True)
            try:
                os.link(input_source, temporary)
            except OSError:
                with input_source.open('rb') as incoming, temporary.open('wb') as outgoing:
                    shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            if digest(temporary) != metadata['source_sha256']:
                temporary.unlink(missing_ok=True)
                raise ValueError('Source changed during preparation')
        else:
            export_csv(repository, temporary)
        temporary.replace(source_csv)
    result = {'snapshot_id': metadata['snapshot_id'], 'accepted_rows': metadata['accepted_rows'],
              'quarantined_rows': metadata['quarantined_rows'],
              'source_csv_sha256': digest(source_csv),
              'CAPA_SOURCE_SNAPSHOT': str(snapshot), 'CAPA_SOURCE_CSV': str(source_csv)}
    write_json(data / '.setup/source.json', result)
    print(f"Source ready: {metadata['accepted_rows']:,} records; {metadata['quarantined_rows']:,} held at import.", flush=True)
    return result


def allowed_sites(setting, indexed):
    if setting == 'auto':
        return sorted(indexed)
    try:
        sites = json.loads(setting)
    except ValueError:
        raise ValueError('MATCH_ALLOWED_SITES_JSON must be auto or a JSON array of indexed sites.') from None
    if not isinstance(sites, list) or not sites or any(not isinstance(site, str) or not site for site in sites):
        raise ValueError('No sites authorised. Configure an explicit site list, or auto for all locally indexed sites.')
    if not set(sites) <= set(indexed):
        raise ValueError('A configured allowed site is absent from this index. Review the site list.')
    return sorted(set(sites))


def index_contract(root):
    files = ['src/match_v1/' + name + '.py' for name in ('core', 'index', 'ingest', 'search', 'scope')]
    files += ['src/capa_match/build.py', 'src/capa_match/identifiers.py']
    return {'versions': {name: importlib.metadata.version(name) for name in ('chromadb', 'onnxruntime', 'sentence-transformers')},
            'code': {name: hashlib.sha256((root / name).read_text(encoding='utf8').encode()).hexdigest()
                     for name in files if (root / name).is_file()}}


def seed_index(archive, target, *, snapshot_id=None):
    """Expand a verified Linux index without accepting links or escaping paths."""
    from match_v1.index import load_index_metadata
    staging = target.with_name('seed-' + uuid4().hex)
    staging.mkdir(parents=True)
    try:
        with tarfile.open(archive, 'r:gz') as stream:
            for member in stream:
                if not member.isfile() and not member.isdir():
                    raise ValueError('Index archive must contain regular files/directories only')
                destination = contained(staging, member.name)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with stream.extractfile(member) as incoming, destination.open('wb') as outgoing:
                        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        manifest, _, _, _ = load_index_metadata(staging)
        if snapshot_id is not None:
            # Only the versioned bundle's explicitly validated source identities
            # reach this path. Record text, chunks and vectors remain exact.
            manifest['source_snapshot_id'] = snapshot_id
            if 'source_snapshot_id' in manifest.get('source_statistics', {}):
                manifest['source_statistics']['source_snapshot_id'] = snapshot_id
            write_json(staging / 'manifest.json', manifest)
            write_json(staging / manifest['storage_directory'] / 'manifest.json', manifest)
        staging.rename(target)
    finally:
        if staging.exists():
            # Only this function's new staging directory is eligible for cleanup.
            assert staging.resolve().parent == target.resolve().parent and staging.name.startswith('seed-')
            shutil.rmtree(staging)


def prepare_index(root, data, assets, source, limit, batch_size, rebuild, bundle=None):
    from . import build
    from match_v1.index import load_index_metadata, open_chroma

    model = assets['embedding_model']
    if os.getenv('MATCH_MODEL_NAME', model) != model:
        raise ValueError('This launcher provisions the bundled MPNet checkpoint. Use a separate installation for another model.')
    policy = {'snapshot': source['snapshot_id'], 'model': model, 'revision': assets['embedding_revision'],
              'runtime': 'onnx-fp32', 'division': os.getenv('MATCH_INDEX_DIVISION',''), 'status_scope': os.getenv('MATCH_INDEX_STATUS_SCOPE','all'), 'limit': limit, 'chunk_size': 500, 'chunk_overlap': 50,
              'text_fields': ['DETAILED_DESCRIPTION', 'CONCLUSION']}
    contract = index_contract(root)
    identity = hashlib.sha256(json.dumps([policy, contract], sort_keys=True).encode()).hexdigest()
    target = data / 'match-managed' / identity
    os.environ.update({key: source[key] for key in ('CAPA_SOURCE_SNAPSHOT', 'CAPA_SOURCE_CSV')})
    os.environ.update(CAPA_DATA_DIR=str(data), MATCH_MODEL_CACHE=str(data / 'models/huggingface'),
                      MATCH_MODEL_NAME=model, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    existing = (target / 'manifest.json').is_file()
    seed = assets.get('bundled_index', {})
    seed_policy = {**seed.get('policy', {}), 'snapshot': source['snapshot_id']}
    compatible = (seed_policy == policy and seed.get('contract') == contract
                  and source['snapshot_id'] in seed.get('equivalent_snapshot_ids', [seed.get('policy', {}).get('snapshot')]))
    if not existing and not rebuild and compatible:
        if target.exists():
            # Retain incomplete builds before publishing the verified bundle.
            assert target.resolve().parent == (data / 'match-managed').resolve() and target.name == identity
            target.rename(target.with_name('interrupted-' + identity[:12] + '-' + uuid4().hex[:8]))
        print('Preparing the verified full Linux index for the bundled source...', flush=True)
        archive = contained(bundle or data, Path(seed['path']).relative_to('data'))
        seed_index(archive, target, snapshot_id=source['snapshot_id'])
        existing = True
    if not existing or rebuild:
        print('Building the real Linux search index. First use can take time; chunk progress follows.', flush=True)
        arguments = ['--index-dir', str(target), '--limit', str(limit), '--batch-size', str(batch_size),
                     '--runtime', 'onnx-fp32', '--model', model, '--division', policy['division'], '--status-scope', policy['status_scope']]
        if rebuild or target.exists():
            arguments.append('--rebuild')  # Retry an interrupted first generation without deleting it.
        build.main(arguments, use_dotenv=False)
    else:
        print('Reusing the completed search index for this source and setup.', flush=True)
    manifest, records, chunks, _ = load_index_metadata(target)
    if (manifest.get('source_snapshot_id') != source['snapshot_id'] or manifest['embedding_backend'] != 'MPNet'
            or manifest.get('embedding_runtime') != 'onnx-fp32' or manifest['model_name'] != model
            or manifest.get('embedding_dimensions') != 768):
        raise ValueError('Prepared index does not match its source/model/runtime policy.')
    if open_chroma(target, manifest=manifest).count() != len(chunks):
        raise ValueError('Index vector count differs from the saved chunks. Rerun with -RebuildIndex.')
    sites = allowed_sites(os.getenv('MATCH_ALLOWED_SITES_JSON', 'auto'), manifest['site_counts'])
    result = {**source, 'MATCH_INDEX_DIR': str(target), 'MATCH_MODEL_NAME': model,
              'MATCH_MODEL_CACHE': str(data / 'models/huggingface'), 'MATCH_ALLOWED_SITES_JSON': json.dumps(sites),
              'identity': identity, 'records': len(records), 'chunks': len(chunks), 'sites': len(sites),
              'generation': manifest['storage_directory'], 'limit': limit}
    write_json(data / '.setup/match.json', result)
    print(f"Index ready: {len(records):,} deviations, {len(chunks):,} chunks, {len(sites)} allowed sites.", flush=True)
    return result


def prepare(root, data, bundle, *, source=None, sheet=None, limit=0, batch_size=16, rebuild=False):
    if limit < 0 or batch_size < 1:
        raise ValueError('Limit must be nonnegative and batch size must be positive.')
    data = data.resolve()
    (data / '.setup').mkdir(parents=True, exist_ok=True)
    with FileLock(str(data / '.setup/match-prepare.lock'), timeout=0):
        assets = prepare_files(root, data, bundle)
        prepared_source = prepare_source(data, source, sheet)
        return prepare_index(root, data, assets, prepared_source, limit, batch_size, rebuild, bundle)


def api(base, path, body=None):
    request = Request(base.rstrip('/') + '/api/v1/match' + path,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={'Content-Type': 'application/json'})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=300) as response:
            return json.load(response)
    except HTTPError as error:
        detail = json.load(error).get('detail', f'HTTP {error.code}')
        raise RuntimeError(f'MATCH API: {detail}') from None


def verify(base):
    info, source = api(base, '/index'), api(base, '/source')
    if not info['ready'] or info['mock_embeddings'] or info['source_snapshot_id'] != source['snapshot_id']:
        raise ValueError('MATCH is not using a real index for the configured source.')
    sites = [item['site'] for item in api(base, '/sites')['sites']]
    if not sites:
        raise ValueError('No searchable sites are configured.')
    query = 'HPLC system suitability failed during analytical testing'
    one = api(base, '/search', {'query': query, 'site': sites[0], 'top_k': 3})
    all_sites = api(base, '/search', {'query': query, 'all_sites': True, 'top_k': 3})
    if not one['results'] or not all_sites['results'] or any(row['site'] != sites[0] for row in one['results']):
        raise ValueError('Real search verification returned no results or crossed a site boundary.')
    if set(all_sites['searched_sites']) != set(sites):
        raise ValueError('All-sites search did not use the configured site scope.')
    report = {'ready': True, 'source_snapshot_id': source['snapshot_id'], 'accepted_source_rows': source['accepted_rows'],
              'records': info['records'], 'chunks': info['chunks'], 'sites': len(sites),
              'real_embeddings': True, 'single_site_search': True, 'all_sites_search': True}
    print(json.dumps(report), flush=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    preparation = commands.add_parser('prepare')
    preparation.add_argument('--root', type=Path, default=Path('/app'))
    preparation.add_argument('--data', type=Path, default=Path('/data'))
    preparation.add_argument('--bundle', type=Path, default=Path('/bundle'))
    preparation.add_argument('--source', type=Path)
    preparation.add_argument('--sheet')
    preparation.add_argument('--limit', type=int, default=0)
    preparation.add_argument('--batch-size', type=int, default=16)
    preparation.add_argument('--rebuild-index', action='store_true')
    verification = commands.add_parser('verify')
    verification.add_argument('--url', default='http://127.0.0.1:8102')
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        prepare(args.root, args.data, args.bundle, source=args.source, sheet=args.sheet,
                limit=args.limit, batch_size=args.batch_size, rebuild=args.rebuild_index)
    else:
        verify(args.url)


if __name__ == '__main__':
    main()
