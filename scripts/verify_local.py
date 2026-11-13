"""Exercise the real standalone MATCH service without external model API calls."""
import argparse
import json
from pathlib import Path
import time
import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8102')
    args = parser.parse_args()
    base = args.url.rstrip('/') + '/api/v1/match'
    report = {'steps': [], 'real_embeddings': False}
    def call(method, path, **kwargs):
        start = time.perf_counter()
        response = requests.request(method, base + path, timeout=240, **kwargs)
        response.raise_for_status()
        report['steps'].append({'path': path, 'seconds': round(time.perf_counter() - start, 3)})
        return response.json()
    try:
        info = call('GET', '/index')
        assert info['ready'] and not info['mock_embeddings']
        assert info['embedding_backend'] == 'MPNet'
        report['real_embeddings'] = True
        metadata = call('GET', '/source')
        assert metadata['accepted_rows'] + metadata['quarantined_rows'] == metadata['source_rows']
        assert metadata['snapshot_id'] == info['source_snapshot_id']
        sites = [item['site'] for item in call('GET', '/sites')['sites']]
        site = 'TRO (Toronto)' if 'TRO (Toronto)' in sites else sites[0]
        query = 'HPLC bracket standard failed system suitability during testing'
        result = call('POST', '/search', json={'query': query, 'site': site, 'top_k': 5})
        assert result['results'] and all(row['site'] == site for row in result['results'])
        first = result['results'][0]
        detail = call('GET', '/matched-records/' + first['pr_id'], params={'site': first['site']})
        assert detail['pr_id'] == first['pr_id'] and detail['source_snapshot_id'] == metadata['snapshot_id']
        assert detail['indexed_field_texts']
        combined = call('POST', '/search', json={'query': query, 'all_sites': True, 'top_k': 10})
        assert combined['all_sites'] and set(combined['searched_sites']) == set(sites)
        assert all(row['site'] in sites for row in combined['results'])
        for row in combined['results']:
            expected = 100 * (.7 * row['semantic_score'] + .2 * row['bm25_score'] + .1 * row['jaccard_score'])
            assert abs(expected - row['score_percent']) < .02
        denied = requests.post(base + '/search', json={'query': query, 'site': 'FICTIONAL-NOT-ALLOWED'}, timeout=20)
        assert denied.status_code == 403
        report.update(result='passed', indexed_records=info['records'], chunks=info['chunks'], sites=len(sites),
                      accepted_source_rows=metadata['accepted_rows'], source_snapshot_id=metadata['snapshot_id'],
                      site_results=len(result['results']), all_site_results=len(combined['results']), runtime=info['embedding_runtime'])
    except Exception as error:
        report.update(result='failed', error=str(error))
        raise
    finally:
        local = Path(__file__).resolve().parents[1] / '.local'
        local.mkdir(exist_ok=True)
        (local / 'live-match.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps({'result': report.get('result'), 'real_embeddings': report['real_embeddings']}), flush=True)


if __name__ == '__main__':
    main()
