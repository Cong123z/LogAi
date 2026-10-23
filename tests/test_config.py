from logai2.config import load_config


def test_env_overrides_yaml(tmp_path, monkeypatch):
    p = tmp_path / "c.yaml"; p.write_text("start:\n  days_back: 5\nfloors:\n  ERROR: 7\n")
    monkeypatch.setenv("LOGAI2_ES_HOSTS", "http://a:9200,http://b:9200")
    cfg = load_config(str(p))
    assert cfg.start.days_back == 5 and cfg.floors["ERROR"] == 7 and cfg.floors["WARN"] == 10
    assert cfg.elasticsearch.hosts == ["http://a:9200", "http://b:9200"]


def test_api_token_from_env(monkeypatch):
    monkeypatch.delenv("LOGAI2_API_TOKEN", raising=False)
    assert load_config(None).web.api_token == ""
    monkeypatch.setenv("LOGAI2_API_TOKEN", "")
    assert load_config(None).web.api_token == ""
    monkeypatch.setenv("LOGAI2_API_TOKEN", "tok")
    assert load_config(None).web.api_token == "tok"


def test_embedding_api_key_from_env(monkeypatch):
    monkeypatch.delenv("LOGAI2_EMBEDDING_API_KEY", raising=False)
    assert load_config(None).embedding.api_key == ""
    monkeypatch.setenv("LOGAI2_EMBEDDING_API_KEY", "k")
    assert load_config(None).embedding.api_key == "k"


def test_embedding_model_from_env(monkeypatch):
    monkeypatch.delenv("LOGAI2_EMBEDDING_MODEL", raising=False)
    assert load_config(None).embedding.model == "bge-m3"
    monkeypatch.setenv("LOGAI2_EMBEDDING_MODEL", "BAAI/bge-m3")
    assert load_config(None).embedding.model == "BAAI/bge-m3"
