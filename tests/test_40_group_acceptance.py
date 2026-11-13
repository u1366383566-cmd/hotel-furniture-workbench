from backend.app.database import get_db
from backend.app.task_worker import worker_instance


def test_forty_groups_generate_all_four_slots(client):
    response = client.post('/api/batches', headers={'X-User-Role': 'leader'}, json={
        'name': '40组隔离验收', 'enabled_series': ['modern_business'],
        'target_archived_count': 40, 'designs_per_series': 1,
        'series_counts': {'modern_business': {'primary': 40, 'standby': 0}},
        'standby_designs_per_series': 0, 'auto_start_panorama': True,
        'warning_budget': 400, 'hard_limit_budget': 500,
    })
    assert response.status_code == 200, response.text
    bid = response.json()['id']
    for _ in range(40):
        assert worker_instance.process_next_task()
    designs = client.get(f'/api/designs/batch/{bid}').json()
    assert len(designs) == 40
    for design in designs:
        pano = design['slots']['panorama']['current_version_id']
        reviewed = client.post(f"/api/designs/{design['id']}/review_panorama",
                               json={'action':'approve','expected_version_id':pano})
        assert reviewed.status_code == 200, reviewed.text
        started = client.post(f"/api/designs/{design['id']}/start_downstream")
        assert started.status_code == 200, started.text
    for _ in range(120):
        assert worker_instance.process_next_task()
    with get_db() as conn:
        rows = conn.execute("SELECT status,COUNT(*) n FROM tasks WHERE batch_id=? GROUP BY status", (bid,)).fetchall()
        assert [(r['status'], r['n']) for r in rows] == [('completed', 160)]
        ledger = conn.execute("""SELECT status,COUNT(*) n FROM budget_ledger
            WHERE batch_id=? AND category='image_generation' AND ledger_event_type='generation_request'
            GROUP BY status""", (bid,)).fetchall()
        assert [(r['status'], r['n']) for r in ledger] == [('settled', 160)]
