def test_design_listing_supports_gzip(client):
    response = client.post('/api/batches', headers={'X-User-Role': 'leader'}, json={
        'name': '压缩测试', 'enabled_series': ['modern_business'],
        'target_archived_count': 1, 'designs_per_series': 1,
        'standby_designs_per_series': 0, 'auto_start_panorama': False,
    })
    assert response.status_code == 200, response.text
    batch = response.json()
    listing = client.get(f"/api/designs/batch/{batch['id']}", headers={'Accept-Encoding': 'gzip'})
    assert listing.status_code == 200
    assert listing.headers.get('content-encoding') == 'gzip'
    assert len(listing.json()) == 1


def test_preview_revalidates_after_authorization(client, monkeypatch):
    from backend.app import security
    from backend.app.task_worker import worker_instance
    response = client.post('/api/batches', headers={'X-User-Role': 'leader'}, json={
        'name': '预览缓存测试', 'enabled_series': ['modern_business'],
        'target_archived_count': 1, 'designs_per_series': 1,
        'standby_designs_per_series': 0, 'auto_start_panorama': True,
    })
    assert response.status_code == 200
    assert worker_instance.process_next_task()
    design = client.get(f"/api/designs/batch/{response.json()['id']}").json()[0]
    url = f"/api/media/preview/{design['slots']['panorama']['current_version_id']}"
    first = client.get(url)
    assert first.status_code == 200 and first.headers['cache-control'] == 'private, max-age=604800, immutable'
    assert 'content-encoding' not in first.headers
    cached = client.get(url, headers={'If-None-Match': first.headers['etag']})
    assert cached.status_code == 304 and not cached.content
    monkeypatch.setattr(security, 'AUTH_MODE', 'trusted_proxy')
    assert client.get(url, headers={'If-None-Match': first.headers['etag']}).status_code == 401
