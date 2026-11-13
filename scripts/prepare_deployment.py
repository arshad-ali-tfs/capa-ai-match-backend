"""Prepare a code-only Databricks upload, without source data, models or secrets."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil


def main():
    root = Path(__file__).resolve().parents[1]
    target = root / '.local/deployment' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target.mkdir(parents=True, exist_ok=False)
    shutil.copytree(root / 'src', target / 'src', ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.egg-info'))
    for name in ['app.yaml', 'pyproject.toml', 'requirements-lock.txt', 'README.md', 'DEPLOYMENT.md', 'FEATURE_PARITY.md', 'VERIFICATION.md']:
        shutil.copy2(root / name, target / name)
    (target / 'requirements.txt').write_text('-r requirements-lock.txt\n.[databricks]\n', encoding='utf-8')
    files = [path for path in target.rglob('*') if path.is_file()]
    assert all(path.stat().st_size < 10_000_000 for path in files)
    manifest = {'service': 'match', 'files': {path.relative_to(target).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in files},
                'source_csv_models_databases_credentials_excluded': True}
    (target / 'deployment-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'deployment_source': str(target), 'files': len(files), 'large_runtime_artifacts_excluded': True}))


if __name__ == '__main__':
    main()
