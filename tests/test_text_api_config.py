def test_text_model_configuration_is_independent_and_secret(client):
    headers = {"X-User-Role": "leader", "X-User-ID": "tester"}
    cfg = {"base_url": "http://text.example", "model": "planner", "timeout_seconds": 45, "api_key": "private-key"}
    response = client.put('/api/system/text_api', json=cfg, headers=headers)
    assert response.status_code == 200, response.text
    response = client.get('/api/system/text_api', headers=headers)
    assert response.status_code == 200
    assert response.json()['model'] == 'planner'
    assert response.json()['has_key'] is True
    assert 'private-key' not in response.text
    cfg['api_key'] = ''
    assert client.put('/api/system/text_api', json=cfg, headers=headers).status_code == 200
    assert client.get('/api/system/text_api', headers=headers).json()['has_key'] is True
    assert client.get('/api/system/text_api', headers={'X-User-Role': 'operator'}).status_code == 403
