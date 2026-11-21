"""Prepare the complete local retrieval path without external model calls."""
from match_v1.contracts import SearchFilters


def prepare_search(service, progress=None):
    core = getattr(service, 'service', service)
    core.embedder.encode(['Historical deviation search.'])
    core.facets(all_sites=True)
    core._explorer.ensure_source_documents()
    titles = core._explorer.prepared_titles.precompute(progress)
    # Exercise vector retrieval, filtered keyword statistics and title lookup,
    # rather than marking an encoder-only warm-up as fully prepared.
    core.search('Historical deviation investigation', all_sites=True, top_k=1, force_ranked=True)
    sites = tuple(row['site'] for row in core.sites())
    for division in sorted({record.get('division', '') for record in core.records.values()}):
        if not division:
            continue
        filters = SearchFilters(divisions=[division]).model_dump(mode='json')
        selected = {key for key, record in core.records.items() if record['site'] in core.allowed_sites and core._explorer.accepts(record, filters)}
        core._scope_corpus(sites, selected)
    return {**titles, 'keyword_populations': len(core._corpus_caches)}
