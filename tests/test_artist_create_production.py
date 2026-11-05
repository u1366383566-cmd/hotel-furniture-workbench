from backend.app import security


def test_trusted_proxy_artist_creates_only_automatic_task(client, monkeypatch):
    monkeypatch.setattr(security, 'AUTH_MODE', 'trusted_proxy')
    payload = {
        'name': '美工生产任务', 'enabled_series': ['modern_business'],
        'target_archived_count': 1, 'smart_planning': True, 'auto_workflow': True,
        'auto_start_panorama': False, 'series_counts': {'modern_business': {'primary': 1, 'standby': 0}},
        'warning_budget': 1, 'hard_limit_budget': 2,
    }
    assert client.post('/api/batches', json=payload).status_code == 401
    headers = {'X-User-Role': 'artist', 'X-User-ID': 'artist-test'}
    response = client.post('/api/batches', headers=headers, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()['auto_workflow'] is True
    assert response.json()['warning_budget'] >= 5
    assert client.post('/api/batches', headers=headers, json={**payload, 'auto_workflow': False}).status_code == 403
    assert client.put('/api/workbench/budget-settings', headers=headers, json={'warning_per_group': 5, 'hard_limit_per_group': 6}).status_code == 403
