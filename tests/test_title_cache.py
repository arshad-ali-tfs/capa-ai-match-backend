from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest
from match_v1 import title_cache
from test_explorer import explorer


def prepared(monkeypatch,tmp_path,service=None):
    monkeypatch.setattr(title_cache,'model_fingerprint',lambda _: 'a'*64)
    return title_cache.TitleVectors(service or explorer(),{},tmp_path)


def test_persisted_title_vectors_preserve_results_without_reencoding(monkeypatch,tmp_path):
    first=prepared(monkeypatch,tmp_path);report=first.precompute()
    assert report['computed']>0 and list(tmp_path.glob('*.npz'))
    service=explorer();second=prepared(monkeypatch,tmp_path,service)
    service.embedder.encode=lambda _:pytest.fail('An exact, model-matched title must be reused')
    again=second.precompute()
    assert again['computed']==0 and again['reused']==report['titles']
    assert first.vectors.keys()==second.vectors.keys()
    for key in first.vectors:np.testing.assert_array_equal(first.vectors[key],second.vectors[key])


def test_changed_title_is_recomputed_and_old_source_ids_are_not_added(monkeypatch,tmp_path):
    first=prepared(monkeypatch,tmp_path);first.precompute()
    service=explorer();service.records={'new-id':{**service.records['101'],'pr_id':'new-id','record_name':'A new fictional event title'}}
    second=prepared(monkeypatch,tmp_path,service);calls=[];encode=service.embedder.encode
    def track(texts):calls.extend(texts);return encode(texts)
    service.embedder.encode=track;result=second.precompute()
    assert calls==['A new fictional event title'] and result['computed']==1
    assert set(second.vectors)=={('new-id','A new fictional event title')}


@pytest.mark.parametrize('payload',[b'corrupted cache',b'PK\x03\x04broken zip',b'\x93NUMPY'])
def test_corrupt_cache_falls_back_to_original_title_calculation(monkeypatch,tmp_path,payload):
    (tmp_path/('a'*64+'.npz')).write_bytes(payload)
    store=prepared(monkeypatch,tmp_path);value=store.precompute()
    assert value['computed']==value['titles']>0
    recovered=prepared(monkeypatch,tmp_path);assert recovered.precompute()['computed']==0


def test_changed_model_assets_invalidate_persistent_vectors(tmp_path):
    (tmp_path/'weights.onnx').write_bytes(b'fictional weights')
    (tmp_path/'tokenizer.json').write_text('fictional tokenizer',encoding='utf-8')
    model=SimpleNamespace(_session=SimpleNamespace(_model_path=str(tmp_path/'weights.onnx')),
                          _tokenizer=SimpleNamespace(name_or_path=str(tmp_path)),max_length=384)
    service=SimpleNamespace(embedder=model,manifest={'model_name':'fictional-model','embedding_runtime':'onnx-fp32'})
    before=title_cache.model_fingerprint(service)
    (tmp_path/'weights.onnx').write_bytes(b'changed fictional model weights')
    after=title_cache.model_fingerprint(service)
    assert before!=after
    (tmp_path/'tokenizer.json').write_text('changed tokenizer',encoding='utf-8')
    assert title_cache.model_fingerprint(service)!=after


def test_invalid_cache_metadata_cannot_break_source_title_matching(monkeypatch,tmp_path):
    first=prepared(monkeypatch,tmp_path);first.precompute()
    path=next(tmp_path.glob('*.npz'))
    with np.load(path,allow_pickle=False) as data:keys,vectors=data['keys'],data['vectors']
    np.savez(path,keys=keys,vectors=vectors,metadata=np.asarray('[]'))
    replacement=prepared(monkeypatch,tmp_path)
    result=replacement.precompute()
    assert result['computed']==result['titles']>0 and replacement.vectors


def test_name_only_model_identity_never_enables_disk_reuse(tmp_path):
    store=title_cache.TitleVectors(explorer(),{},tmp_path)
    assert store.precompute()['computed']>0 and not list(tmp_path.iterdir())


def test_unwritable_optional_disk_cache_preserves_in_memory_title_matching(monkeypatch,tmp_path):
    blocked=tmp_path/'a-file-instead-of-a-directory';blocked.write_text('fictional',encoding='utf-8')
    store=prepared(monkeypatch,blocked)
    result=store.precompute()
    assert result['computed']==result['titles']>0 and store.vectors
    assert store.save() is False


def test_cached_and_original_title_ranking_have_the_same_order_and_scores(monkeypatch,tmp_path):
    service=explorer();query='HPLC equipment calibration failed'
    expected=service.search(query,all_sites=True,mode='semantic')
    another=explorer();another.facets(all_sites=True)
    store=prepared(monkeypatch,tmp_path,another);store.precompute()
    another._explorer.prepared_titles=title_cache.TitleVectors(another,another._explorer.title_vectors,tmp_path)
    actual=another.search(query,all_sites=True,mode='semantic')
    assert actual['results']==expected['results']
