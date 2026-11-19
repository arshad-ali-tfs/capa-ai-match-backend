"""A prepared local model must load without online tokenizer/cache checks."""
import json
import sys
from types import SimpleNamespace
import huggingface_hub
from match_v1.core import ONNXMPNetEmbedder


def test_cached_model_artifacts_and_tokenizer_load_locally(tmp_path, monkeypatch):
    for name, value in [('1_Pooling/config.json', {'pooling_mode_mean_tokens': True}),
                        ('sentence_bert_config.json', {'max_seq_length': 384}), ('config.json', {})]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    (tmp_path / 'onnx').mkdir()
    (tmp_path / 'onnx/model_O2.onnx').write_bytes(b'fictional-onnx-only-a-test-double')
    requested = []
    def download(model, *, filename, cache_dir, local_files_only=False):
        assert local_files_only, 'A cache hit must not trigger an online check'
        requested.append(filename)
        return str(tmp_path / filename)
    def tokenizer(path, *, local_files_only):
        assert path == str(tmp_path) and local_files_only
        return 'fictional-tokenizer'
    session = object()
    monkeypatch.setattr(huggingface_hub, 'hf_hub_download', download)
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=tokenizer)))
    monkeypatch.setitem(sys.modules, 'onnxruntime', SimpleNamespace(SessionOptions=SimpleNamespace,
                         InferenceSession=lambda *args, **kwargs: session))
    model = ONNXMPNetEmbedder()
    model._load()
    model._load()
    assert model._session is session and model._tokenizer == 'fictional-tokenizer'
    assert len(requested) == 4  # no reloading or network activity on the second call
