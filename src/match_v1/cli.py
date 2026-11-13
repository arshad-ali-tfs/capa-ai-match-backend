"""MATCH v1 command-line entrypoint: build, sites, search, serve, selfcheck."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .core import DEFAULT_MODEL, MockEmbedder, make_mpnet_embedder, split_chunks
from .index import build_index, load_index_metadata, open_chroma
from .ingest import load_gtw
from .search import MatchService
from .testing import MemoryCollection


def _attach_search(index_dir: str, allowed_sites: set[str]):
    manifest, records, chunks, capas = load_index_metadata(index_dir)
    if manifest['embedding_backend'] == 'MPNet':
        model = os.environ.get('MATCH_MODEL_NAME', manifest['model_name'])
        if model != manifest['model_name']:
            raise ValueError('Query model differs from index. Use the exact model or rebuild the index.')
        embedder = make_mpnet_embedder(model, runtime=manifest.get('embedding_runtime', 'torch'))
    else:
        embedder = MockEmbedder()
    return MatchService(manifest=manifest, records=records, chunks=chunks, capas=capas,
                        collection=open_chroma(index_dir, manifest=manifest), embedder=embedder,
                        allowed_sites=allowed_sites)


def _selfcheck():
    demo = Path(__file__).parent.parent / 'demo' / 'demo_gtw.csv'
    records, capas, stats = load_gtw(demo, division='DPD', limit=0)
    print('Fixture:', stats['indexed_deviations'], 'closed deviations,', stats['linked_capas'], 'CAPA links')
    model = MockEmbedder()
    index = MemoryCollection()
    chunks = []
    for rec in records:
        for seq, txt in enumerate(split_chunks(rec['text'])):
            chunks.append({'chunk_id': f"{rec['pr_id']}_chunk_{seq}", 'pr_id': rec['pr_id'],
                           'site': rec['site'], 'division': rec['division'], 'doc': txt})
    index.upsert(ids=[c['chunk_id'] for c in chunks],
                 documents=[c['doc'] for c in chunks],
                 metadatas=[{k: c[k] for k in ('pr_id', 'site', 'division')} for c in chunks],
                 embeddings=model.encode([c['doc'] for c in chunks]).tolist())
    manifest = {'model_name': model.model_name, 'embedding_backend': 'MOCK_UNFIT_FOR_REAL_SEARCH',
                'chunk_size': 500, 'chunk_overlap': 50,
                'site_counts': {name: sum(r['site'] == name for r in records)
                                for name in {r['site'] for r in records}}}
    service = MatchService(manifest=manifest, records={r['pr_id']: r for r in records},
                           chunks=chunks, capas=capas, collection=index, embedder=model,
                           allowed_sites={'TRO (Toronto)'})
    results = service.search('HPLC laboratory testing equipment system suitability failure',
                             site='TRO (Toronto)', top_k=3)
    assert results['result_count'] >= 1 and all(r['site'] == 'TRO (Toronto)' for r in results['results'])
    assert any(r['related_capa_count'] > 0 for r in results['results']), 'No related CAPA in demo results'
    try:
        service.search('HPLC laboratory testing equipment system suitability failure',
                       site='GRC (Greenville, NC)')
    except PermissionError:
        print('Site access restriction: PASS')
    else:
        raise AssertionError('Unauthorized site search did not fail')
    print('End-to-end OFFLINE smoke check: PASS (MOCK embeddings, not MPNet or ChromaDB)')
    for result in results['results']:
        print('  ', result['pr_id'], result['site'], result['score_percent'],
              'related CAPAs', result['related_capa_count'])


def main(argv=None):
    parser = argparse.ArgumentParser(description='CAPA.AI MATCH Local v1; OpenAI not required')
    sub = parser.add_subparsers(dest='command', required=True)
    build = sub.add_parser('build', help='Build persistent MPNet+Chroma index from GTW CSV')
    build.add_argument('--gtw', required=True, help='Path to GTW flat-file CSV')
    build.add_argument('--division', default='DPD', help='DPD, DSD, CTD, Global, or empty string for all')
    build.add_argument('--site', help='Optionally limit indexed corpus to a single site')
    build.add_argument('--limit', type=int, default=200, help='Default 200 deviations; 0=all eligible')
    build.add_argument('--index-dir', default='local_index')
    build.add_argument('--model', default=os.environ.get('MATCH_MODEL_NAME', DEFAULT_MODEL),
                       help='MPNet checkpoint name or local path; defaults to MATCH_MODEL_NAME or base MPNet')
    build.add_argument('--batch-size', type=int, default=16)
    build.add_argument('--runtime', choices=('torch', 'onnx-fp32'), default='torch')
    build.add_argument('--chunk-size', type=int, default=500)
    build.add_argument('--chunk-overlap', type=int, default=50)
    build.add_argument('--text-field', action='append', help='Repeat to combine text fields; default DETAILED_DESCRIPTION')
    build.add_argument('--mock-embeddings', action='store_true', help='OFFLINE DEMO ONLY - no MPNet semantics')
    build.add_argument('--replace', action='store_true', help='Build and activate a new index generation')

    sites = sub.add_parser('sites', help='Display indexed sites and record counts')
    sites.add_argument('--index-dir', default='local_index')
    search = sub.add_parser('search', help='Run scoped hybrid search in terminal')
    search.add_argument('--index-dir', default='local_index')
    search.add_argument('--site', required=True, help='Exact site string from `sites`')
    search.add_argument('--query', required=True)
    search.add_argument('--top-k', type=int, default=10)
    serve = sub.add_parser('serve', help='Start local-only web UI / FastAPI')
    serve.add_argument('--index-dir', default='local_index')
    serve.add_argument('--site', action='append', required=True,
                       help='Allowed site for local server, repeat for multiple')
    serve.add_argument('--port', type=int, default=8000)
    sub.add_parser('selfcheck', help='No-network functional demo with MOCK embeddings')
    args = parser.parse_args(argv)

    if args.command == 'build':
        if args.batch_size <= 0:
            parser.error('--batch-size must be positive')
        records, capas, stats = load_gtw(args.gtw, division=args.division, site=args.site,
                                          limit=args.limit,
                                          text_fields=tuple(args.text_field or ['DETAILED_DESCRIPTION']))
        print('Selected', len(records), 'eligible deviations, linked', stats['linked_capas'], 'CAPAs')
        manifest = build_index(args.index_dir, records, capas, stats,
                               model_name=args.model, mock_embeddings=args.mock_embeddings,
                               batch_size=args.batch_size, chunk_size=args.chunk_size,
                               chunk_overlap=args.chunk_overlap, replace=args.replace, runtime=args.runtime)
        print('Index built:', json.dumps({k: manifest[k] for k in ('record_count', 'chunk_count', 'embedding_backend', 'site_counts')}, indent=2))
    elif args.command == 'sites':
        manifest, _, _, _ = load_index_metadata(args.index_dir)
        print('Embedding backend:', manifest['embedding_backend'])
        for name, count in manifest['site_counts'].items():
            print(f'{name}: {count} deviations')
    elif args.command == 'search':
        service = _attach_search(args.index_dir, {args.site})
        result = service.search(args.query, site=args.site, top_k=args.top_k)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.command == 'serve':
        os.environ['MATCH_INDEX_DIR'] = args.index_dir
        os.environ['MATCH_ALLOWED_SITES_JSON'] = json.dumps(args.site)
        try:
            import uvicorn
        except ImportError as exc:
            raise RuntimeError('Install requirements.txt to run the web server.') from exc
        print('Local web app: http://127.0.0.1:%d' % args.port)
        print('Server-allowed site(s):', args.site)
        uvicorn.run('match_v1.api:app', host='127.0.0.1', port=args.port, reload=False)
    elif args.command == 'selfcheck':
        _selfcheck()


if __name__ == '__main__':
    main()
