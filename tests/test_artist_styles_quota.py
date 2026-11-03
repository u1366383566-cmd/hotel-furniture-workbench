import httpx
import pytest
from backend.app.database import get_db
from backend.app.routers import workbench


def test_artist_can_create_and_edit_series(client):
    headers = {'X-User-Role': 'artist', 'X-User-ID': 'artist_test'}
    created = client.post('/api/system/series_rules', headers=headers, json={'series_id': 'artist_style_test', 'name': '美工风格', 'rule': {}})
    assert created.status_code == 200
    updated = client.put('/api/system/series_rules/artist_style_test', headers=headers, json={'description': '温暖自然', 'materials': ['白橡木']})
    assert updated.status_code == 200
    rules = client.get('/api/system/series_rules', headers=headers).json()
    assert rules['artist_style_test']['materials'] == ['白橡木']
    with get_db(immediate=True) as conn:
        audit = conn.execute("SELECT actor_name FROM audit_logs WHERE action='update_series_rule' AND actor_name='artist_test'").fetchone()
        assert audit
        conn.execute("DELETE FROM series_rule_configs WHERE series_id='artist_style_test'")


def test_quota_success_and_failures(client, monkeypatch):
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE image_api_configs SET enabled=1, base_url='http://upstream/v1', api_key='private-key' WHERE id='default'")
    def response(payload, status=200):
        return httpx.Response(status, json=payload, request=httpx.Request('GET', 'http://upstream/api/quota'))
    def good(url, **kwargs):
        assert url == 'http://upstream/api/quota'
        assert kwargs['headers']['Authorization'] == 'Bearer private-key'
        return response({'quota': 17, 'accounts': ['secret']})
    monkeypatch.setattr(workbench.httpx, 'get', good)
    for role in ('operator', 'artist', 'leader', 'admin'):
        data = client.get('/api/workbench/upstream-quota', headers={'X-User-Role': role}).json()
        assert data['status'] == 'ok' and data['quota'] == 17 and data['checked_at']
        assert 'private-key' not in str(data) and 'accounts' not in data
    for payload, status in [({'quota': -1}, 200), ({'quota': '10'}, 200), ({}, 200), ({'detail': 'private-key'}, 401)]:
        monkeypatch.setattr(workbench.httpx, 'get', lambda *a, p=payload, s=status, **k: response(p, s))
        assert client.get('/api/workbench/upstream-quota').json() == {'status': 'unavailable', 'quota': None, 'checked_at': None}
    def timeout(*args, **kwargs):
        raise httpx.TimeoutException('private-key')
    monkeypatch.setattr(workbench.httpx, 'get', timeout)
    assert client.get('/api/workbench/upstream-quota').json()['status'] == 'unavailable'
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE image_api_configs SET enabled=0 WHERE id='default'")
    assert client.get('/api/workbench/upstream-quota').json()['quota'] is None


def test_artist_can_generate_series_with_language_model(client, monkeypatch):
    from backend.app.routers import system
    from backend.app.config import SERIES_RULES
    monkeypatch.setattr(system, 'load_text_api_config', lambda conn: {'enabled': True, 'has_key': True, 'model': 'test', 'timeout_seconds': 5})
    monkeypatch.setattr(system, 'chat_completion_real', lambda *a, **k: 'model response')
    monkeypatch.setattr(system, 'parse_llm_rules', lambda raw: {**SERIES_RULES['modern_business'], 'series_id': 'artist_ai_test', 'name': 'AI 风格'})
    result = client.post('/api/system/series_rules/generate', headers={'X-User-Role':'operator', 'X-User-ID':'artist_ai'}, json={'style_brief':'温暖原木'})
    assert result.status_code == 200, result.text
    assert result.json()['series_id'] == 'artist_ai_test'
    with get_db(immediate=True) as conn:
        assert conn.execute("SELECT updated_by FROM series_rule_configs WHERE series_id='artist_ai_test'").fetchone()['updated_by'] == 'artist_ai'
        conn.execute("DELETE FROM series_rule_configs WHERE series_id='artist_ai_test'")
