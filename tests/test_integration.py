"""Opt-in real MPNet + disk-backed Chroma checks. Never substitute mock vectors."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from match_v1 import api
import numpy as np
from match_v1.core import MPNetEmbedder, ONNXMPNetEmbedder, make_mpnet_embedder
from match_v1.index import build_index, load_index_metadata, open_chroma
from match_v1.ingest import load_gtw
from match_v1.search import MatchService

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(os.environ.get('MATCH_RUN_INTEGRATION') != '1',
                                 reason='Set MATCH_RUN_INTEGRATION=1 to use real MPNet and Chroma')]

DEMO = Path(__file__).parent / 'fixtures' / 'demo_gtw.csv'
QUERY = 'The chromatograph failed its suitability check when analysing drug substance batches with a bracket standard.'
SITE = 'TRO (Toronto)'


@pytest.fixture(scope='module', params=['torch', 'onnx-fp32'])
def real_index(tmp_path_factory, request):
    path = tmp_path_factory.mktemp('mpnet') / 'index'
    records, capas, stats = load_gtw(DEMO, limit=0)
    manifest = build_index(path, records, capas, stats, runtime=request.param)
    assert manifest['embedding_backend'] == 'MPNet'
    return path


def test_real_semantic_search_and_linked_capas(real_index):
    manifest, records, chunks, capas = load_index_metadata(real_index)
    service = MatchService(manifest=manifest, records=records, chunks=chunks, capas=capas,
                           collection=open_chroma(real_index),
                           embedder=make_mpnet_embedder(manifest['model_name'], runtime=manifest['embedding_runtime']), allowed_sites={SITE})
    response = service.search(QUERY, site=SITE, top_k=3)
    assert response['mock_embeddings'] is False
    assert response['results'][0]['pr_id'] == '101'
    assert response['results'][0]['related_capas'][0]['pr_id'] == '901'
    assert all(row['site'] == SITE for row in response['results'])


def test_reopen_and_search_in_fresh_offline_process(real_index):
    env = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    env.pop('OPENAI_API_KEY', None)
    result = subprocess.run([sys.executable, '-m', 'match_v1.cli', 'search',
                             '--index-dir', str(real_index), '--site', SITE,
                             '--query', QUERY, '--top-k', '3'],
                            env=env, text=True, capture_output=True, timeout=180)
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response['mock_embeddings'] is False
    assert response['results'][0]['pr_id'] == '101'


def test_real_api_with_persisted_index(real_index, monkeypatch):
    api.get_service.cache_clear()
    monkeypatch.setenv('MATCH_INDEX_DIR', str(real_index))
    monkeypatch.setenv('MATCH_ALLOWED_SITES_JSON', json.dumps([SITE]))
    with TestClient(api.app) as client:
        assert client.get('/api/health').json()['embedding_backend'] == 'MPNet'
        response = client.post('/api/search', json={'query': QUERY, 'site': SITE, 'top_k': 3})
        assert response.status_code == 200
        assert response.json()['results'][0]['pr_id'] == '101'
        assert client.post('/api/search', json={'query': QUERY, 'site': 'GRC (Greenville, NC)'}).status_code == 403
    api.get_service.cache_clear()


def test_onnx_float32_matches_torch_vectors():
    texts = [QUERY, 'An operator found powder leaking from incompletely sealed capsule shells.']
    reference = MPNetEmbedder().encode(texts)
    optimized = ONNXMPNetEmbedder().encode(texts)
    assert np.min(np.sum(reference * optimized, axis=1)) > .99999
    assert np.max(np.abs(reference - optimized)) < .00001
