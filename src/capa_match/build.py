"""Build and activate a MATCH index tied to the configured source snapshot."""
import argparse
import json
import os
from pathlib import Path
from dotenv import dotenv_values, set_key
from capa_match.cli import ROOT


def main(argv=None, *, use_dotenv=True):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rebuild', '--replace', dest='rebuild', action='store_true', help='Publish a new generation while retaining previous files')
    parser.add_argument('--gtw', type=Path, help='Optional CSV identical to the configured snapshot export; import new data with bootstrap.py first')
    parser.add_argument('--index-dir', type=Path, help='Defaults to data/match-<snapshot ID>')
    parser.add_argument('--division', help='Default all divisions; use DPD, DSD, CTD or Global to restrict')
    parser.add_argument('--status-scope', choices=('completed','ongoing','all'), help='Default all statuses; completed and cancelled visibility is controlled at search time')
    parser.add_argument('--site', help='Optionally index only this exact site')
    parser.add_argument('--limit', type=int, help='Platform default 0 means all eligible deviations')
    parser.add_argument('--model', help='Checkpoint name or local path; default MATCH_MODEL_NAME or base MPNet')
    parser.add_argument('--batch-size', type=int, help='Default 16')
    parser.add_argument('--runtime', choices=('torch', 'onnx-fp32'), help='Platform default onnx-fp32')
    parser.add_argument('--chunk-size', type=int, help='Character window size; default 500')
    parser.add_argument('--chunk-overlap', type=int, help='Character overlap; default 50')
    parser.add_argument('--text-field', action='append', help='Repeat for separate fields; platform default description + conclusion')
    parser.add_argument('--mock-embeddings', action='store_true', help='Offline demo only; no MPNet semantics')
    args = parser.parse_args(argv)
    config = dict(os.environ)
    if use_dotenv and os.getenv('CAPA_ENV', 'local') == 'local':
        config.update({key: value for key, value in dotenv_values(ROOT / '.env').items() if value is not None})
    for key in ['CAPA_SOURCE_SNAPSHOT', 'CAPA_SOURCE_CSV', 'CAPA_DATA_DIR', 'MATCH_INDEX_DIR', 'MATCH_MODEL_CACHE']:
        value = config.get(key)
        if value and not value.startswith(('dbfs:', '/Volumes/')):
            candidate = Path(value)
            config[key] = str((ROOT / candidate).resolve() if not candidate.is_absolute() else candidate.resolve())
    if not config.get('CAPA_SOURCE_SNAPSHOT') or not config.get('CAPA_SOURCE_CSV'):
        parser.error('Import a source with match-import first.')
    os.environ.update(config)
    from capa_match.infrastructure.data.importer import sha256
    from capa_match.infrastructure.data.repository import SqliteRecordRepository
    from match_v1.ingest import load_gtw
    from match_v1.core import DEFAULT_MODEL, MockEmbedder, make_mpnet_embedder, split_chunks
    from match_v1.index import build_index, storage_path
    from capa_match.identifiers import SourceIdentifiers
    repository = SqliteRecordRepository(Path(config['CAPA_SOURCE_SNAPSHOT']))
    metadata = repository.metadata()
    try:
        SourceIdentifiers(repository)
    except ValueError as error:
        parser.error(str(error))
    configured_source = Path(config['CAPA_SOURCE_CSV']).resolve(strict=True)
    source = args.gtw.resolve(strict=True) if args.gtw else configured_source
    with source.open('rb') as handle:
        if handle.read(128).startswith(b'version https://git-lfs.github.com/spec/v1'):
            parser.error('The source is a Git LFS pointer. Run git lfs pull first.')
    if source != configured_source and sha256(source) != sha256(configured_source):
        parser.error('--gtw must match the configured snapshot export. Import this source with bootstrap.py first.')
    limit = args.limit if args.limit is not None else 0
    batch_size = args.batch_size if args.batch_size is not None else 16
    chunk_size = args.chunk_size if args.chunk_size is not None else 500
    chunk_overlap = args.chunk_overlap if args.chunk_overlap is not None else 50
    if limit < 0 or batch_size < 1:
        parser.error('--limit must be >= 0 and --batch-size must be positive')
    try:
        split_chunks('', chunk_size, chunk_overlap)
    except ValueError as error:
        parser.error(str(error))
    target = args.index_dir.resolve() if args.index_dir else Path(config['CAPA_DATA_DIR']) / ('match-' + metadata['snapshot_id'])
    if not args.index_dir and config.get('MATCH_INDEX_DIR'):
        configured=Path(config['MATCH_INDEX_DIR'])
        active=configured/'manifest.json'
        if active.is_file() and json.loads(active.read_text(encoding='utf-8')).get('source_snapshot_id')==metadata['snapshot_id']:
            target=configured
    settings_requested = args.mock_embeddings or any(getattr(args, name) is not None for name in (
        'division', 'status_scope', 'site', 'limit', 'model', 'batch_size', 'runtime', 'chunk_size', 'chunk_overlap', 'text_field'))

    def activate(manifest):
        if not use_dotenv:
            return  # Container setup publishes configuration in mounted storage.
        set_key(str(ROOT / '.env'), 'MATCH_INDEX_DIR', str(target))
        set_key(str(ROOT / '.env'), 'MATCH_ALLOWED_SITES_JSON', json.dumps(sorted(manifest['site_counts'])))
        if manifest.get('embedding_backend') == 'MPNet':
            set_key(str(ROOT / '.env'), 'MATCH_MODEL_NAME', manifest['model_name'])

    manifest_path = target / 'manifest.json'
    if manifest_path.is_file() and not args.rebuild:
        existing = json.loads(manifest_path.read_text(encoding='utf8'))
        if existing.get('source_snapshot_id') != metadata['snapshot_id']:
            raise RuntimeError('Existing index has a different source version')
        if settings_requested:
            parser.error('Index already exists. Use --rebuild (or --replace) to apply build settings.')
        activate(existing)
        print('Matching source index already exists. Restart services to activate it. Use --rebuild for a new generation.')
        return
    runtime = args.runtime or 'onnx-fp32'
    model_name = args.model or config.get('MATCH_MODEL_NAME') or DEFAULT_MODEL
    records, capas, stats = load_gtw(source, division=args.division if args.division is not None else config.get('MATCH_INDEX_DIVISION',''),
                                    status_scope=args.status_scope or config.get('MATCH_INDEX_STATUS_SCOPE','all'),
                                    site=args.site, limit=limit,
                                    text_fields=tuple(args.text_field or ('DETAILED_DESCRIPTION', 'CONCLUSION')))
    stats = {**stats, 'source_snapshot_id': metadata['snapshot_id']}
    embedder = MockEmbedder() if args.mock_embeddings else make_mpnet_embedder(model_name, batch_size, runtime=runtime)
    manifest = build_index(target, records, capas, stats, model_name=model_name,
                           mock_embeddings=args.mock_embeddings, embedder=embedder, runtime=runtime,
                           batch_size=batch_size, chunk_size=chunk_size, chunk_overlap=chunk_overlap,
                           replace=args.rebuild)
    manifest['source_snapshot_id'] = metadata['snapshot_id']
    # Both the active pointer and retained generation identify their source.
    generation_manifest = storage_path(target, manifest) / 'manifest.json'
    generation_manifest.write_text(json.dumps(manifest, indent=2), encoding='utf8')
    pending = manifest_path.with_suffix('.snapshot.tmp')
    pending.write_text(json.dumps(manifest, indent=2), encoding='utf8')
    pending.replace(manifest_path)
    activate(manifest)
    print('MATCH index built. Restart the services to activate it.')
    if args.mock_embeddings:
        print('OFFLINE DEMO: mock embeddings were used; these results do not use MPNet.')


if __name__ == '__main__':
    main()
