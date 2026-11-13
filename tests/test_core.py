import numpy as np
import pytest

from match_v1.core import BM25, MockEmbedder, clean_for_index, jaccard, normalize_pr_id, split_chunks, tokenize, unit_rows, weighted_score


def test_normalize_id():
    assert normalize_pr_id('1208.0') == '1208'
    assert normalize_pr_id('00128') == '128'
    assert normalize_pr_id('00128.00') == '128'
    assert normalize_pr_id('') == ''
    assert normalize_pr_id('ABC-11') == 'ABC-11'


def test_source_text_keeps_equipment_and_numbers():
    assert '#219' in clean_for_index('<b>HPLC #219</b><br>lot A37')
    assert 'lot A37' in clean_for_index('<b>HPLC #219</b><br>lot A37')


def test_chunk_size_and_coverage():
    text = ' '.join(f'sample-{i:03d}' for i in range(220))
    chunks = split_chunks(text, size=200, overlap=25)
    assert len(chunks) > 5
    assert all(0 < len(x) <= 200 for x in chunks)
    assert 'sample-000' in chunks[0]
    assert 'sample-219' in chunks[-1]
    assert split_chunks('short') == ['short']


def test_default_chunks_preserve_exact_overlap_and_all_characters():
    text = clean_for_index(' '.join(f'Equipment-{i} failure during inspection.' for i in range(80)))
    chunks = split_chunks(text)
    assert all(len(chunk) == 500 for chunk in chunks[:-1])
    assert all(left[-50:] == right[:50] for left, right in zip(chunks, chunks[1:]))
    assert chunks[0] + ''.join(chunk[50:] for chunk in chunks[1:]) == text


def test_jaccard_correct_direction():
    assert jaccard(['equipment', 'failure'], ['equipment', 'failure']) == 1.0
    assert jaccard(['equipment'], ['packaging']) == 0.0
    assert jaccard([], []) == 0.0
    assert weighted_score(.8, .5, 1) == pytest.approx(.76)
    assert weighted_score(.8, .5, 0) == pytest.approx(.66)


def test_bm25_exact_term_preferred():
    bm = BM25([tokenize('hplc failure bracket standard'), tokenize('tablet packaging foil'), tokenize('hplc system fault')])
    score = bm.scores(tokenize('hplc bracket'))
    assert score[0] > score[1] and score[0] > score[2]
    assert np.all(np.isfinite(score))


def test_mock_vectors_are_deterministic_and_normalized():
    embedder = MockEmbedder()
    em1 = embedder.encode(['HPLC failure', 'packaging label'])
    em2 = embedder.encode(['HPLC failure', 'packaging label'])
    assert em1.shape == (2, 768)
    assert np.allclose(em1, em2)
    assert np.allclose(np.linalg.norm(em1, axis=1), 1.)


def test_zero_norm_rejected():
    with pytest.raises(ValueError):
        unit_rows(np.zeros((2, 768), dtype='float32'))
