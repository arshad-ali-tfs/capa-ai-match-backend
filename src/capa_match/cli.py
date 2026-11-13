"""Service-owned configuration, launcher and immutable CSV/XLSX import."""
import argparse
import json
import os
from pathlib import Path
from dotenv import load_dotenv, set_key

def project_root():
    configured = os.getenv('CAPA_PROJECT_ROOT')
    if configured:
        return Path(configured).resolve()
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / 'src/capa_match').is_dir():
        return candidate
    # Installed wheels use the launch directory, not site-packages, for app state.
    return Path.cwd().resolve()


ROOT = project_root()


def configure():
    if os.getenv('CAPA_ENV', 'local') == 'local':
        load_dotenv(ROOT / '.env', override=False)
    defaults = {'CAPA_DATA_DIR': './data', 'CAPA_CACHE_DIR': './data/cache', 'MATCH_INDEX_DIR': './data/match-index', 'MATCH_MODEL_CACHE': './data/models/huggingface'}
    for key, value in defaults.items():
        os.environ.setdefault(key, value)
    for key in [*defaults, 'CAPA_SOURCE_SNAPSHOT', 'CAPA_SOURCE_CSV', 'AUTHOR_GTW_CSV']:
        value = os.getenv(key)
        if value and not value.startswith(('dbfs:', '/Volumes/')):
            candidate = Path(value)
            os.environ[key] = str((ROOT / candidate).resolve() if not candidate.is_absolute() else candidate.resolve())
    Path(os.environ['CAPA_DATA_DIR']).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')
    os.environ.setdefault('ANONYMIZED_TELEMETRY', 'False')
    if os.environ['MATCH_MODEL_CACHE'].endswith('.zip'):
        from .infrastructure.storage import local_directory
        os.environ['MATCH_MODEL_CACHE'] = str(local_directory(os.environ['MATCH_MODEL_CACHE']))



def serve():
    configure()
    import uvicorn
    uvicorn.run('capa_match.main:app', host=os.getenv('MATCH_HOST', '127.0.0.1'),
                port=int(os.getenv('MATCH_PORT', os.getenv('DATABRICKS_APP_PORT', '8102'))))


def import_source(argv=None):
    configure()
    from .infrastructure.data.importer import import_snapshot
    from .infrastructure.data.repository import SqliteRecordRepository
    from .infrastructure.data.export import export_csv
    parser = argparse.ArgumentParser(description='Import CSV/XLSX locally without external model calls.')
    parser.add_argument('source', type=Path)
    parser.add_argument('--sheet')
    args = parser.parse_args(argv)
    if args.source.suffix.lower() == '.csv':
        with args.source.open('rb') as handle:
            if handle.read(128).startswith(b'version https://git-lfs.github.com/spec/v1'):
                parser.error('The source is a Git LFS pointer. Run git lfs pull first.')
    snapshot = import_snapshot(args.source, Path(os.environ['CAPA_DATA_DIR']) / 'sources', sheet=args.sheet)
    repository = SqliteRecordRepository(snapshot)
    metadata = repository.metadata()
    source_csv = snapshot.with_suffix('.csv')
    if not source_csv.exists():
        export_csv(repository, source_csv)
    values = {'CAPA_SOURCE_SNAPSHOT': str(snapshot), 'CAPA_SOURCE_CSV': str(source_csv)}

    for key, value in values.items():
        set_key(str(ROOT / '.env'), key, value)
    print(json.dumps({'snapshot_id': metadata['snapshot_id'], 'rows': metadata['source_rows'],
                      'accepted': metadata['accepted_rows'], 'held': metadata['quarantined_rows']}))


if __name__ == '__main__':
    serve()
