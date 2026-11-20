"""Opaque generation identities, without exposing host storage locations."""
import hashlib
import json


def index_generation(manifest):
    identity=[manifest.get('source_snapshot_id'),manifest.get('storage_directory'),
              manifest.get('created_utc'),manifest.get('source_statistics',{}).get('text_normalization_version')]
    return hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
