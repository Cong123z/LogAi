import math

import pytest

from logai2.embed import EmbeddingError, embed, literal
from tests.fake_http import FakeHttp, axis, embeddings_handler


@pytest.fixture
def server():
    servers = []

    def make(handler):
        servers.append(FakeHttp(handler))
        return servers[-1]
    yield make
    for s in servers:
        s.close()


def test_batch_round_trip_in_order_and_unit_length(server):
    s = server(embeddings_handler({"a": axis(0, x=3.0), "b": axis(1)}))
    va, vb = embed(s.url + "/", "bge-m3", ["a", "b"])
    assert va[0] == 1.0 and vb[1] == 1.0 and math.isclose(sum(x * x for x in va), 1.0)
    assert s.calls == [("/v1/embeddings", {"input": ["a", "b"], "model": "bge-m3"})]
    assert literal([0.5, -1.0]) == "[0.5,-1]"


def test_errors_raise(server):
    assert pytest.raises(EmbeddingError, embed, server(lambda p, b: (500, {"error": "x"})).url, "m", ["a"])
    short = server(lambda p, b: (200, {"data": [{"index": 0, "embedding": [1.0, 0.0]}]}))
    assert pytest.raises(EmbeddingError, embed, short.url, "m", ["a"])
    missing = server(lambda p, b: (200, {"data": []}))
    assert pytest.raises(EmbeddingError, embed, missing.url, "m", ["a"])


def test_api_key_sent_as_bearer_and_full_endpoint_url_accepted(server):
    s = server(embeddings_handler({}))
    embed(s.url, "bge-m3", ["a"])
    embed(s.url + "/gw/v1/embeddings", "bge-m3", ["a"], api_key="k1")
    assert [c[0] for c in s.calls] == ["/v1/embeddings", "/gw/v1/embeddings"]
    assert s.auth == [None, "Bearer k1"]


def test_401_message_has_no_key(server):
    s = server(lambda p, b: (401, {"error": "unauthorized"}))
    with pytest.raises(EmbeddingError) as e:
        embed(s.url, "m", ["a"], api_key="secret-key")
    assert "401" in str(e.value) and "secret-key" not in str(e.value)


def test_retries_server_errors_not_client_errors(server):
    n = {"calls": 0}

    def flaky(p, b):
        n["calls"] += 1
        return (503, {"error": "busy"}) if n["calls"] < 3 else embeddings_handler({})(p, b)
    s = server(flaky)
    assert len(embed(s.url, "m", ["a"], max_retries=2, backoff_seconds=0)) == 1 and n["calls"] == 3
    denied = server(lambda p, b: (403, {"error": "forbidden"}))
    assert pytest.raises(EmbeddingError, embed, denied.url, "m", ["a"], max_retries=3, backoff_seconds=0)
    assert len(denied.calls) == 1


def test_dimension_checked(server):
    s = server(lambda p, b: (200, {"data": [{"index": 0, "embedding": [1.0, 0.0]}]}))
    assert embed(s.url, "m", ["a"], dims=2) == [[1.0, 0.0]]
    assert pytest.raises(EmbeddingError, embed, s.url, "m", ["a"], dims=1024)
