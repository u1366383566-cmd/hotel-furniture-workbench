import json

from backend.app.planning_service import generate_design_spec_from_rule


def test_random_combination_avoids_previous_and_warns_on_limited_rules():
    rule = {'name': '测试系列', 'template_version': 'v1', 'headboard_types': ['床屏一', '床屏二'],
            'bed_frames': ['床体一', '床体二'], 'nightstands': ['柜一', '柜二'],
            'wardrobe_types': ['衣柜一', '衣柜二'], 'materials': ['木一', '木二'],
            'color_palette': ['色一', '色二']}
    first = generate_design_spec_from_rule('test', 0, rule)
    second = generate_design_spec_from_rule('test', 1, rule, history=[first])
    assert first['variation_combination'] != second['variation_combination']
    assert not second['variation_warning']
    rule['materials'] = ['木一']
    assert generate_design_spec_from_rule('test', 2, rule)['variation_warning']


def test_ai_suggestions_do_not_persist_and_validate(client, monkeypatch):
    from backend.app.routers import system
    monkeypatch.setattr(system, 'load_text_api_config', lambda conn: {'enabled': True, 'has_key': True, 'model': 'test', 'timeout_seconds': 5})
    monkeypatch.setattr(system, 'chat_completion_real', lambda *a: json.dumps({'options': ['新木材', '已有木材', '新木材']}))
    headers = {'X-User-Role': 'operator'}
    before = client.get('/api/system/series_rules', headers=headers).json()['modern_business']
    draft = {**before, 'materials': ['已有木材']}
    response = client.post('/api/system/series_rules/modern_business/suggestions', headers=headers,
                           json={'field': 'materials', 'count': 20, 'rule': draft})
    assert response.status_code == 200 and response.json()['options'] == ['新木材']
    assert client.get('/api/system/series_rules', headers=headers).json()['modern_business'] == before
    assert client.post('/api/system/series_rules/modern_business/suggestions', headers=headers,
                       json={'field': 'materials', 'count': 101, 'rule': draft}).status_code == 422
    monkeypatch.setattr(system, 'chat_completion_real', lambda *a: 'invalid')
    assert client.post('/api/system/series_rules/modern_business/suggestions', headers=headers,
                       json={'field': 'materials', 'count': 1, 'rule': draft}).status_code == 502


def test_history_uses_other_tasks_and_excludes_eliminated(client):
    from backend.app.database import get_db
    from backend.app.planning_service import series_history
    with get_db(immediate=True) as conn:
        conn.execute("INSERT INTO batches (id,name,enabled_series) VALUES ('variation-test','test','[]')")
        for design_id, status in [('kept', 'draft'), ('removed', 'eliminated')]:
            conn.execute("INSERT INTO designs (id,batch_id,series_id,design_code,status,structured_spec) VALUES (?,?,?,?,?,?)",
                         (design_id, 'variation-test', 'modern_business', design_id, status, json.dumps({'headboard': design_id})))
        history = series_history(conn, 'modern_business')
        assert [item['headboard'] for item in history] == ['kept']


def test_planning_context_limits_large_option_lists_but_keeps_constraints():
    from backend.app.routers.smart_planning import _planning_rule_context
    rule = {'materials': [f'木材{i}' for i in range(100)], 'headboard_types': [f'床屏{i}' for i in range(100)],
            'forbidden_elements': ['雕花'], 'description': '现代商务'}
    context = _planning_rule_context(rule, {'main_materials': ['木材88'], 'headboard': '床屏99'})
    assert context['materials'] == ['木材88'] and context['headboard_types'] == ['床屏99']
    assert context['forbidden_elements'] == ['雕花'] and context['description'] == '现代商务'
