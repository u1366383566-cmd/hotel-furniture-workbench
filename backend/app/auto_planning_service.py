"""Persistent per-design planning jobs. Image reservations remain transactional."""
import asyncio
import json
import os
import uuid

from .database import get_db
from .security import UserContext


def queue_replacement(conn, batch_id, series_id, source_design_id=None):
    from .planning_service import generate_design_spec_from_rule
    row = conn.execute('SELECT approved_series_rules_json FROM batches WHERE id=?', (batch_id,)).fetchone()
    rules = json.loads(row['approved_series_rules_json'] or '{}')
    source = conn.execute('SELECT series_rule_snapshot_json FROM designs WHERE id=? AND batch_id=? AND series_id=?', (source_design_id, batch_id, series_id)).fetchone() if source_design_id else None
    if source and source['series_rule_snapshot_json']:
        rules[series_id] = json.loads(source['series_rule_snapshot_json'])
    if series_id not in rules:
        return None
    index = conn.execute('SELECT COUNT(*) AS n FROM designs WHERE batch_id=? AND series_id=?', (batch_id, series_id)).fetchone()['n']
    spec = generate_design_spec_from_rule(series_id, index, rules[series_id])
    design_id = f'des_{uuid.uuid4().hex[:12]}'
    code = f'AUTO-{uuid.uuid4().hex[:8].upper()}'
    conn.execute("INSERT INTO designs (id,batch_id,series_id,design_code,status,structured_spec,template_version,planning_status,series_rule_snapshot_json) VALUES (?,?,?,?,'draft',?,?,'pending',?)",
                 (design_id,batch_id,series_id,code,json.dumps(spec,ensure_ascii=False),spec['template_version'],json.dumps(rules[series_id],ensure_ascii=False)))
    for slot_type in ('panorama','white_isolated','twin_bed','nightstand_closeup'):
        conn.execute("INSERT INTO slots (id,design_id,slot_type,status) VALUES (?,?,?,'empty')", (f'slt_{uuid.uuid4().hex}',design_id,slot_type))
    conn.execute('INSERT INTO planning_jobs (id,batch_id,design_id) VALUES (?,?,?)', (f'plan_{uuid.uuid4().hex}',batch_id,design_id))
    return design_id


def process_next_plan():
    from .routers.smart_planning import generate, ReplanRequest, _design, _candidates
    from .routers.batches import _queue_initial_panorama
    with get_db(immediate=True) as conn:
        job = conn.execute("""SELECT p.* FROM planning_jobs p JOIN batches b ON b.id=p.batch_id
            JOIN designs d ON d.id=p.design_id WHERE p.status='queued' AND b.auto_workflow=1
            AND b.status='active' AND b.is_circuit_broken=0 AND d.status='draft'
            ORDER BY p.created_at,p.id LIMIT 1""").fetchone()
        if not job:
            return False
        conn.execute("UPDATE planning_jobs SET status='running', attempts=attempts+1, updated_at=datetime('now','localtime') WHERE id=?", (job['id'],))
    try:
        with get_db() as conn:
            current = _design(conn,job['design_id'])
            already_planned = current['planning_status']=='ready'
            replan_count = int(job['replan_count'] or 0)
        if not already_planned:
            # ``replan_count`` is persisted before each corrective model call,
            # so a restart cannot silently grant more than two replans.
            while True:
                feedback = '请调整家具造型，减少与已有款的相似。' if replan_count else ''
                if replan_count:
                    with get_db(immediate=True) as conn:
                        conn.execute("UPDATE planning_jobs SET replan_count=? WHERE id=?", (replan_count, job['id']))
                generate(job['design_id'], ReplanRequest(feedback=feedback), UserContext('auto_planner','admin'))
                with get_db() as conn:
                    similar = _candidates(conn,_design(conn,job['design_id']))
                if not similar or replan_count >= 2:
                    if similar and replan_count >= 2:
                        with get_db(immediate=True) as conn:
                            row = _design(conn, job['design_id'])
                            spec = json.loads(row['structured_spec'])
                            spec['planning_review_warning'] = {
                                'reason': 'similarity_after_max_replans',
                                'replan_count': replan_count,
                                'similarities': similar,
                            }
                            conn.execute(
                                "UPDATE designs SET structured_spec=?, planning_status='ready', updated_at=datetime('now','localtime') WHERE id=?",
                                (json.dumps(spec, ensure_ascii=False), job['design_id']),
                            )
                    break
                replan_count += 1
        with get_db(immediate=True) as conn:
            row = conn.execute('SELECT d.design_code,d.status,b.status AS batch_status,b.is_circuit_broken FROM designs d JOIN batches b ON b.id=d.batch_id WHERE d.id=?', (job['design_id'],)).fetchone()
            if not row or row['status'] != 'draft' or row['batch_status'] != 'active' or row['is_circuit_broken']:
                raise ValueError('任务状态已变化，暂不能启动全景')
            existing = conn.execute("SELECT 1 FROM tasks WHERE design_id=? AND task_type='generate_panorama' LIMIT 1", (job['design_id'],)).fetchone()
            if not existing:
                _queue_initial_panorama(conn, job['batch_id'], job['design_id'], row['design_code'])
            conn.execute("UPDATE planning_jobs SET status='done',error=NULL,updated_at=datetime('now','localtime') WHERE id=?",(job['id'],))
    except Exception as exc:
        message = getattr(exc, 'detail', None) or str(exc) or type(exc).__name__
        with get_db(immediate=True) as conn:
            conn.execute("UPDATE planning_jobs SET status='failed',error=?,updated_at=datetime('now','localtime') WHERE id=?", (str(message)[:500],job['id']))
    return True


class PlannerWorker:
    def __init__(self):
        self.task = None
        self.running = False

    async def start(self):
        with get_db(immediate=True) as conn:
            conn.execute("UPDATE planning_jobs SET status='queued',error='服务重启，待恢复' WHERE status='running'")
        if os.environ.get('WORKBENCH_DISABLE_WORKER_LOOP') == '1':
            return
        self.running = True
        self.task = asyncio.create_task(self.loop())

    async def loop(self):
        while self.running:
            try:
                worked = await asyncio.to_thread(process_next_plan)
                if not worked:
                    await asyncio.sleep(1)
            except Exception:
                await asyncio.sleep(2)

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass


planner_worker = PlannerWorker()
