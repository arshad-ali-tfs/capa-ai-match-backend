"""Filtering may change the population, never the existing BM25 mathematics."""
import random
import numpy as np
import pytest
from match_v1.core import BM25, tokenize
from match_v1.lexical import LexicalIndex
from test_explorer import explorer


@pytest.mark.parametrize('seed',range(8))
def test_compact_scoped_scores_match_rebuilt_bm25_for_varied_populations(seed):
    rng=random.Random(seed)
    vocabulary=['hplc','failure','wash','packaging','temperature','investigation','rare']
    chunks=[{'chunk_id':str(i),'doc':' '.join(rng.choices(vocabulary,k=rng.randrange(0,85)))} for i in range(80)]
    index=LexicalIndex(chunks)
    for size in [0,1,9,40,80]:
        population=rng.sample(chunks,size)
        rebuilt=BM25([tokenize(row['doc']) for row in population]);compact=index.scope(population)
        assert compact.n==rebuilt.n and compact.avg_length==rebuilt.avg_length
        for query in ['hplc failure','temperature temperature','absent','wash rare packaging','']:
            np.testing.assert_allclose(compact.scores(tokenize(query)),rebuilt.scores(tokenize(query)),rtol=1e-14,atol=1e-14)


def test_unselected_documents_cannot_change_a_populations_keyword_scores():
    chunks=[{'chunk_id':'a','doc':'hplc failure failure'},{'chunk_id':'b','doc':'hplc wash'}]
    unrelated=[{'chunk_id':str(i),'doc':'hplc '*100} for i in range(100)]
    expected=LexicalIndex(chunks).scope(chunks).scores(['hplc','failure'])
    actual=LexicalIndex(chunks+unrelated).scope(chunks).scores(['hplc','failure'])
    np.testing.assert_array_equal(actual,expected)


def test_returning_to_a_filter_scope_reuses_its_exact_statistics_and_posting_store():
    service=explorer();sites=tuple(row['site'] for row in service.sites())
    _,first=service._scope_corpus(sites,{'101','102'})
    _,second=service._scope_corpus(sites,{'103','104'})
    _,again=service._scope_corpus(sites,{'101','102'})
    assert again is first and first.index is second.index
    assert first.n!=0 and second.n!=0 and len(service._corpus_caches)==2


def test_many_scopes_remain_bounded_while_sharing_the_large_posting_arrays():
    service=explorer();sites=tuple(row['site'] for row in service.sites())
    keys=list(service.records)
    for i in range(16):service._scope_corpus(sites,{key for bit,key in enumerate(keys) if i&(1<<bit)})
    assert len(service._corpus_caches)<=8
    assert all(value[1].index is service._lexical_index for value in service._corpus_caches.values())
