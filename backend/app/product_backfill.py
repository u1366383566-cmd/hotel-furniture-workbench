"""Create and apply a reviewed historical-numbering manifest.

Run against an isolated backup for preview; apply only after NAS collision checks.
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

from .database import get_db
from .product_numbering import format_product_code


def snapshot(conn):
    return [dict(r) for r in conn.execute("""SELECT d.id,d.batch_id,b.name AS batch_name,
        d.design_code,d.archived_at FROM designs d JOIN batches b ON b.id=d.batch_id
        WHERE d.status='archived' ORDER BY d.id""")]


def make_manifest(conn, batch_ids):
    rows = snapshot(conn)
    chosen = [r for r in rows if r['batch_id'] in batch_ids]
    if set(batch_ids) != {r['batch_id'] for r in chosen}:
        raise ValueError('指定的任务没有已归档组，请检查任务 ID')
    times = []
    for row in chosen:
        try:
            datetime.strptime(row['archived_at'], '%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError) as exc:
            raise ValueError('历史归档时间缺失或无效') from exc
        times.append(row['archived_at'])
    if len(times) != len(set(times)):
        raise ValueError('历史归档存在同秒记录，需先人工确认顺序')
    order = sorted(chosen, key=lambda r: r['archived_at'])
    return {'snapshot': rows, 'batch_ids': sorted(batch_ids),
            'assignments': [{'id': r['id'], 'product_number': n, 'product_code': format_product_code(n), **{k:r[k] for k in ('batch_name','design_code','archived_at')}} for n,r in enumerate(order, 1)]}


def apply_manifest(conn, manifest):
    state = conn.execute('SELECT ready,last_number FROM product_number_state WHERE id=1').fetchone()
    expected = make_manifest(conn, manifest['batch_ids'])
    if manifest != expected:
        raise ValueError('历史数据或清单已变化，请重新生成并确认')
    assigned = conn.execute('SELECT id,product_number FROM designs WHERE product_number IS NOT NULL').fetchall()
    mapping = {r['id']: r['product_number'] for r in assigned}
    desired = {r['id']: r['product_number'] for r in manifest['assignments']}
    if state['ready']:
        if mapping == desired and state['last_number'] == len(desired):
            return
        raise ValueError('编号已启用或发生变化，禁止重新补号')
    if mapping:
        raise ValueError('已有产品编号，请先核对冲突')
    for design_id, number in desired.items():
        conn.execute('UPDATE designs SET product_number=? WHERE id=? AND status=\'archived\'', (number, design_id))
    conn.execute('UPDATE product_number_state SET ready=1,last_number=? WHERE id=1', (len(desired),))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['preview','apply'])
    parser.add_argument('manifest')
    parser.add_argument('--batch', action='append', default=[])
    args = parser.parse_args()
    if args.action == 'preview':
        with get_db() as conn:
            manifest = make_manifest(conn, args.batch)
        Path(args.manifest).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"待确认 {len(manifest['assignments'])} 组，历史快照 {len(manifest['snapshot'])} 组")
    else:
        manifest = json.loads(Path(args.manifest).read_text(encoding='utf-8'))
        with get_db(immediate=True) as conn:
            apply_manifest(conn, manifest)
        print(f"历史编号已核对：{len(manifest['assignments'])} 组")


if __name__ == '__main__':
    main()
