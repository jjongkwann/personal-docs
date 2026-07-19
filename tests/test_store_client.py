"""장수 MCP 프로세스에서 Elasticsearch transport를 host별로 재사용."""

from pkb import store


def test_get_client_reuses_host_pool(monkeypatch):
    created = []
    monkeypatch.setattr(store, "Elasticsearch", lambda host: created.append(host) or object())
    monkeypatch.setattr("pkb.config.settings.es_host", "http://one:9200")
    store._client_for_host.cache_clear()

    first = store.get_client()
    second = store.get_client()

    assert first is second
    assert created == ["http://one:9200"]
    store._client_for_host.cache_clear()


def test_get_client_cache_is_keyed_by_host(monkeypatch):
    monkeypatch.setattr(store, "Elasticsearch", lambda host: {"host": host})
    store._client_for_host.cache_clear()

    monkeypatch.setattr("pkb.config.settings.es_host", "http://one:9200")
    first = store.get_client()
    monkeypatch.setattr("pkb.config.settings.es_host", "http://two:9200")
    second = store.get_client()

    assert first != second
    store._client_for_host.cache_clear()
