import hashlib

from backend.app.database import get_db
from backend.app import nas_upload_service as service


def seed(client, monkeypatch):
    b = client.post('/api/batches', json={'name':'NAS test', 'enabled_series':['modern_business'], 'target_archived_count':1, 'designs_per_series':1, 'standby_designs_per_series':0, 'auto_start_panorama':False}, headers={'X-User-Role':'leader'}).json()
    d = client.get(f"/api/designs/batch/{b['id']}").json()[0]
    with get_db(immediate=True) as c:
        c.execute("UPDATE designs SET status='archived',product_number=9999,archived_at='2026-11-12 10:00:00' WHERE id=?", (d['id'],))
    monkeypatch.setattr(service, '_collect_export_data', lambda *args: ({}, [{'product_number':9999}]))
    monkeypatch.setattr(service, 'render_product_images', lambda *args: [(str(i), f'{i}.png', bytes([i])) for i in range(1,5)])
    return b['id'],d['id']


class FakeRemote:
    def __init__(self):
        self.files = {}
        self.dirs = set()
        self.lose_response = False
        self.puts = 0
    def close(self): pass
    def exists(self, path): return path in self.dirs or path in self.files
    def names(self, path): return {p[len(path)+1:] for p in self.files if p.startswith(path+'/')}
    def mkdir(self, path): self.dirs.add(path)
    def put_new(self, path, data):
        assert path not in self.files
        self.puts += 1
        self.files[path] = data
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError('response lost')
    def verify(self, path, expected_hash, expected_size):
        data = self.files[path]
        if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_hash:
            raise service.NasConflict('different file')
    def publish(self, source, target):
        assert target not in self.dirs
        self.dirs.remove(source);self.dirs.add(target)
        self.files = {(target+p[len(source):] if p.startswith(source+'/') else p):v for p,v in self.files.items()}


def job(did):
    with get_db() as c:
        return dict(c.execute('SELECT * FROM nas_upload_jobs WHERE design_id=?', (did,)).fetchone())


def test_lost_response_retry_checks_remote_before_upload(client, monkeypatch):
    bid,did = seed(client,monkeypatch)
    res=client.post('/api/nas-upload/enqueue', json={'batch_id':bid,'design_ids':[did]})
    assert res.status_code==200 and res.json()['results'][0]['ok']
    remote=FakeRemote();remote.lose_response=True
    assert service.process_one(lambda:remote)
    assert job(did)['status']=='waiting' and remote.puts==1
    client.post('/api/nas-upload/enqueue', json={'batch_id':bid,'design_ids':[did]})
    assert service.process_one(lambda:remote)
    assert job(did)['status']=='success' and remote.puts==4
    assert len(remote.names('TFMK9999'))==4
    with get_db() as c:
        assert c.execute("SELECT count(*) FROM nas_upload_items WHERE job_id=? AND status='verified'",(job(did)['id'],)).fetchone()[0]==4


def test_formal_conflict_never_overwritten(client, monkeypatch):
    bid,did=seed(client,monkeypatch)
    client.post('/api/nas-upload/enqueue',json={'batch_id':bid,'design_ids':[did]})
    remote=FakeRemote();remote.dirs.add('TFMK9999');remote.files['TFMK9999/other.png']=b'other'
    assert service.process_one(lambda:remote)
    assert job(did)['status']=='conflict' and remote.puts==0
    assert remote.files=={'TFMK9999/other.png':b'other'}


def test_unassigned_group_rejected_and_bad_config_stays_unpublished(client, monkeypatch):
    bid,did=seed(client,monkeypatch)
    with get_db(immediate=True) as c:c.execute('UPDATE designs SET product_number=NULL WHERE id=?',(did,))
    r=client.post('/api/nas-upload/enqueue',json={'batch_id':bid,'design_ids':[did]})
    assert not r.json()['results'][0]['ok']
    monkeypatch.delenv('WORKBENCH_NAS_PUBLISH_VERIFIED',raising=False)
    import pytest
    with pytest.raises(RuntimeError,match='尚未配置'):
        service.WebDav()


def test_partial_batch_queue_and_duplicate_click(client, monkeypatch):
    bid,did=seed(client,monkeypatch)
    payload={'batch_id':bid,'design_ids':[did,'unknown']}
    r=client.post('/api/nas-upload/enqueue',json=payload)
    assert [x['ok'] for x in r.json()['results']]==[True,False]
    client.post('/api/nas-upload/enqueue',json=payload)
    with get_db() as c:
        assert c.execute('SELECT count(*) FROM nas_upload_jobs WHERE design_id=?',(did,)).fetchone()[0]==1
    assert client.post('/api/nas-upload/enqueue',json=payload,headers={'X-User-Role':'invalid'}).status_code==403


def test_webdav_publish_refuses_collision():
    import httpx
    seen=[]
    def transport(request):
        seen.append(request)
        return httpx.Response(412,request=request)
    remote=service.WebDav.__new__(service.WebDav)
    remote.base='https://example.invalid/share'
    remote.client=httpx.Client(transport=httpx.MockTransport(transport))
    import pytest
    with pytest.raises(service.NasConflict):remote.publish('.workbench-one','TFMK001')
    assert seen[0].headers['Overwrite']=='F'
    assert seen[0].headers['Destination'].endswith('/TFMK001')
    remote.close()


def test_524_and_bad_readback_are_never_success():
    import httpx
    import pytest
    def transport(request):
        return httpx.Response(524 if request.method=='PUT' else 200,content=b'wrong',request=request)
    remote=service.WebDav.__new__(service.WebDav)
    remote.base='https://example.invalid/share'
    remote.client=httpx.Client(transport=httpx.MockTransport(transport))
    with pytest.raises(RuntimeError,match='524'):
        remote.put_new('temporary/1.png',b'data')
    with pytest.raises(service.NasConflict):
        remote.verify('temporary/1.png',hashlib.sha256(b'data').hexdigest(),4)
    remote.close()


def test_restart_recovers_interrupted_job_and_single_worker(client, monkeypatch, tmp_path):
    import asyncio
    bid,did=seed(client,monkeypatch)
    client.post('/api/nas-upload/enqueue',json={'batch_id':bid,'design_ids':[did]})
    with get_db(immediate=True) as c:
        c.execute("UPDATE nas_upload_jobs SET status='verifying',attempts=1 WHERE design_id=?",(did,))
        jid=c.execute('SELECT id FROM nas_upload_jobs WHERE design_id=?',(did,)).fetchone()['id']
        c.execute('INSERT INTO nas_upload_attempts(job_id,attempt) VALUES (?,1)',(jid,))
    monkeypatch.setenv('WORKBENCH_NAS_PUBLISH_VERIFIED','1')
    monkeypatch.setenv('WORKBENCH_DISABLE_WORKER_LOOP','0')
    monkeypatch.setattr(service,'STORAGE_DIR',tmp_path)
    async def idle():pass
    async def run():
        first=service.NasWorker();second=service.NasWorker()
        first._loop=idle;second._loop=idle
        await first.start();await second.start()
        assert first.lock is not None and second.lock is None
        assert job(did)['status']=='waiting'
        with get_db() as c:
            assert c.execute('SELECT status FROM nas_upload_attempts WHERE job_id=?',(jid,)).fetchone()['status']=='waiting'
        await first.stop();await second.stop()
    asyncio.run(run())
