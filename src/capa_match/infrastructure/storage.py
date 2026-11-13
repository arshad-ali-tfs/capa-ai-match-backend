"""Local files for development, Unity Catalog Volumes for Databricks artifacts."""
from __future__ import annotations
import hashlib
from io import BytesIO
import os
from pathlib import Path
import tempfile
import zipfile


def is_volume(value: str) -> bool:
    return value.startswith('/Volumes/') or value.startswith('dbfs:/Volumes/')


def workspace():
    from databricks.sdk import WorkspaceClient
    return WorkspaceClient()


def read_bytes(location: str) -> bytes:
    if is_volume(location) and not Path(location).is_file():
        result = workspace().files.download(location.removeprefix('dbfs:'))
        with result.contents as stream:
            return stream.read()
    return Path(location).read_bytes()


def write_bytes(location: str, content: bytes):
    if is_volume(location) and os.getenv('CAPA_ENV') == 'databricks':
        workspace().files.upload(location.removeprefix('dbfs:'), BytesIO(content), overwrite=True)
    else:
        path = Path(location)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_bytes(content)
        temporary.replace(path)


def local_file(location: str, *, refresh: bool = False) -> Path:
    if not is_volume(location) or Path(location).is_file():
        return Path(location).resolve(strict=True)
    cache = Path(os.getenv('CAPA_CACHE_DIR', tempfile.gettempdir())) / 'capa-match'
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (hashlib.sha256(location.encode()).hexdigest() + Path(location).suffix)
    if refresh or not target.is_file():
        temporary = target.with_suffix('.partial')
        temporary.write_bytes(read_bytes(location))
        temporary.replace(target)
    return target


def local_directory(location: str) -> Path:
    if not location.endswith('.zip'):
        return Path(location).resolve(strict=True)
    archive = local_file(location)
    destination = archive.with_suffix('.unpacked')
    marker = destination / '.complete'
    if not marker.exists():
        destination.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as zipped:
            for entry in zipped.infolist():
                target = (destination / entry.filename).resolve()
                if not target.is_relative_to(destination.resolve()):
                    raise ValueError('Unsafe artifact archive path')
            zipped.extractall(destination)
        marker.write_text('complete', encoding='utf8')
    return destination
