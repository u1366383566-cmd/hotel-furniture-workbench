import hashlib

from backend.app.database import get_db
from backend.app import nas_upload_service as service


def seed(client, monkeypatch):
    b = client.post('/api/batches', json={'name':'NAS test', 'enabled_series':['modern_business'], 'target_archived_count':1, 'designs_per_series':1, 'standby_designs_per_series':0, 'auto_start_panorama':False}, headers={'X-User-Role':'leader'}).json()
    d = client.get(f"/api/designs/batch/{b['id']}").json()[0]
    with get_db(immediate=True) as c:
        c.execute("UPDATE designs SET status='archived',product_number=9999,archived_at='2026-11-12 10:00:00' WHERE id=?", (d['id'],))
    monkeypatch.setattr(service, '_collect_export_data', lambda *args: ({}, [{'product_number':9999}]))
    types=('white_isolated','twin_bed','nightstand_closeup','panorama')
    monkeypatch.setattr(service, 'render_product_images', lambda *args: [(t, f'{i}.png', bytes([i])) for i,t in enumerate(types,1)])
    monkeypatch.setattr(service, 'validate_upload_source', lambda *args: {t:'version-'+t for t in types})
    return b['id'],d['id']


class FakeRemote:
    def __init__(self):
        self.files = {}
        self.dirs = set()
        self.lose_response = False
        self.lose_mkdir_response = False
        self.puts = 0
    def close(self): pass
    def exists(self, path): return path in self.dirs or path in self.files
    def names(self, path): return {p[len(path)+1:] for p in self.files if p.startswith(path+'/')}
    def mkdir(self, path):
        if path in self.dirs: raise service.NasConflict('exists')
        self.dirs.add(path)
        if self.lose_mkdir_response:
            self.lose_mkdir_response=False
            raise TimeoutError('mkdir response lost')
    def put_new(self, path, data):
        assert path not in self.files
        self.puts += 1
        self.files[path] = data
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError('response lost')
    def verify(self, path, expected_hash, expected_size, data=None, checkpoint=None, progress=None):
        actual = self.files[path]
        if len(actual) != expected_size or hashlib.sha256(actual).hexdigest() != expected_hash:
            raise service.NasConflict('different file')
        if progress: progress(expected_size, '"test-etag"')


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
    assert job(did)['status']=='awaiting_review' and remote.puts==4
    assert len(remote.names('TFMK9999'))==4
    with get_db() as c:
        assert c.execute("SELECT count(*) FROM nas_upload_items WHERE job_id=? AND status='uploaded_unverified'",(job(did)['id'],)).fetchone()[0]==4


def test_formal_conflict_never_overwritten(client, monkeypatch):
    bid,did=seed(client,monkeypatch)
    client.post('/api/nas-upload/enqueue',json={'batch_id':bid,'design_ids':[did]})
    remote=FakeRemote();remote.dirs.add('TFMK9999');remote.files['TFMK9999/other.png']=b'other'
    assert service.process_one(lambda:remote)
    assert job(did)['status']=='conflict' and remote.puts==0
    assert remote.files=={'TFMK9999/other.png':b'other'}


def test_preflight_failure_never_connects_to_nas(client, monkeypatch):
    bid,did=seed(client,monkeypatch)
    client.post('/api/nas-upload/enqueue',json={'batch_id':bid,'design_ids':[did]})
    def invalid(*args):raise ValueError('上传前检查失败：全景版本不一致')
    monkeypatch.setattr(service,'validate_upload_source',invalid)
    called=[]
    def remote():called.append(True);return FakeRemote()
    assert service.process_one(remote)
    assert not called and job(did)['status']=='failed'


def test_all_four_are_sent_without_readback(client, monkeypatch):
    bid,did=seed(client,monkeypatch)
    client.post('/api/nas-upload/enqueue',json={'batch_id':bid,'design_ids':[did]})
    class SlowVerify(FakeRemote):
        def verify(self,*args,**kwargs): raise TimeoutError('readback interrupted')
    remote=SlowVerify()
    assert service.process_one(lambda:remote)
    assert remote.puts==4 and len(remote.names('TFMK9999'))==4
    assert job(did)['status']=='awaiting_review'


def test_unknown_directory_ownership_is_not_assumed(client, monkeypatch):
    bid,did=seed(client,monkeypatch)
    client.post('/api/nas-upload/enqueue',json={'batch_id':bid,'design_ids':[did]})
    remote=FakeRemote();remote.lose_mkdir_response=True
    assert service.process_one(lambda:remote)
    assert job(did)['status']=='waiting' and remote.puts==0
    client.post('/api/nas-upload/enqueue',json={'batch_id':bid,'design_ids':[did]})
    assert service.process_one(lambda:remote)
    assert job(did)['status']=='conflict' and remote.puts==0


def test_unowned_existing_four_images_are_not_assumed_to_be_ours(client, monkeypatch):
    bid,did=seed(client,monkeypatch)
    client.post('/api/nas-upload/enqueue',json={'batch_id':bid,'design_ids':[did]})
    remote=FakeRemote();remote.dirs.add('TFMK9999')
    remote.files={f'TFMK9999/{i}.png':bytes([i]) for i in range(1,5)}
    assert service.process_one(lambda:remote)
    assert job(did)['status']=='conflict' and remote.puts==0


def test_only_manual_confirmation_enables_and_releases_new_archives(client, monkeypatch):
    bid,did=seed(client,monkeypatch)
    other='held-design'
    with get_db(immediate=True) as c:
        c.execute("INSERT INTO designs(id,batch_id,series_id,design_code,status,structured_spec,product_number,archived_at) VALUES (?,?, 'modern_business','HELD','archived','{}',9998,'2026-11-12 10:01:00')",(other,bid))
        c.execute('UPDATE nas_upload_settings SET auto_enabled=0,test_design_id=? WHERE id=1',(did,))
        service.enqueue_upload(c,other,automatic=True)
        service.enqueue_upload(c,did)
    assert job(other)['status']=='held'
    remote=FakeRemote();remote.lose_response=True
    service.process_one(lambda:remote)
    assert job(did)['status']=='waiting' and job(other)['status']=='held'
    assert client.post(f'/api/nas-upload/design/{did}/confirm',json={'attested':True}).status_code==409
    client.post('/api/nas-upload/enqueue',json={'batch_id':bid,'design_ids':[did]})
    service.process_one(lambda:remote)
    assert job(did)['status']=='awaiting_review' and job(other)['status']=='held'
    assert client.post(f'/api/nas-upload/design/{did}/confirm',json={'attested':False}).status_code==400
    assert client.post(f'/api/nas-upload/design/{did}/confirm',json={'attested':True},headers={'X-User-Role':'invalid'}).status_code==403
    assert client.post(f'/api/nas-upload/design/{did}/confirm',json={'attested':True},headers={'X-User-ID':'reviewer'}).status_code==200
    assert client.post(f'/api/nas-upload/design/{did}/confirm',json={'attested':True},headers={'X-User-ID':'reviewer'}).json()['changed'] is False
    assert job(did)['status']=='success' and job(did)['confirmed_by']=='reviewer' and job(other)['status']=='queued'
    with get_db() as c:
        assert c.execute('SELECT auto_enabled FROM nas_upload_settings WHERE id=1').fetchone()[0]==1
        assert c.execute("SELECT count(*) FROM audit_logs WHERE action='nas_manual_confirm' AND actor_name='reviewer'").fetchone()[0]==1


def test_unassigned_group_rejected_and_bad_config_stays_unpublished(client, monkeypatch):
    bid,did=seed(client,monkeypatch)
    with get_db(immediate=True) as c:c.execute('UPDATE designs SET product_number=NULL WHERE id=?',(did,))
    r=client.post('/api/nas-upload/enqueue',json={'batch_id':bid,'design_ids':[did]})
    assert not r.json()['results'][0]['ok']
    monkeypatch.delenv('WORKBENCH_NAS_PUBLISH_VERIFIED',raising=False)
    import pytest
    with pytest.raises(RuntimeError,match='尚未配置'):
        service.WebDav()


def test_unexpected_nas_destination_is_rejected_before_authentication(monkeypatch, tmp_path):
    import pytest
    cfg=tmp_path/'nas.conf'
    cfg.write_text('[zhengzhou-nas]\nurl=https://webdav.seezo.top/错误共享\nuser=x\npass=x\n',encoding='utf-8')
    monkeypatch.setenv('WORKBENCH_NAS_PUBLISH_VERIFIED','1')
    monkeypatch.setenv('WORKBENCH_NAS_RCLONE_CONFIG',str(cfg))
    def forbidden(*args,**kwargs):raise AssertionError('must not retrieve credentials')
    monkeypatch.setattr(service.subprocess,'run',forbidden)
    with pytest.raises(RuntimeError,match='指定的郑州外贸部共享不一致'):service.WebDav()


def test_partial_batch_queue_and_duplicate_click(client, monkeypatch):
    bid,did=seed(client,monkeypatch)
    payload={'batch_id':bid,'design_ids':[did,'unknown']}
    r=client.post('/api/nas-upload/enqueue',json=payload)
    assert [x['ok'] for x in r.json()['results']]==[True,False]
    client.post('/api/nas-upload/enqueue',json=payload)
    with get_db() as c:
        assert c.execute('SELECT count(*) FROM nas_upload_jobs WHERE design_id=?',(did,)).fetchone()[0]==1
    assert client.post('/api/nas-upload/enqueue',json=payload,headers={'X-User-Role':'invalid'}).status_code==403


def test_webdav_upload_refuses_overwrite():
    import httpx
    seen=[]
    def transport(request):
        seen.append(request)
        return httpx.Response(412,request=request)
    remote=service.WebDav.__new__(service.WebDav)
    remote.base='https://example.invalid/share'
    remote.client=httpx.Client(transport=httpx.MockTransport(transport))
    import pytest
    with pytest.raises(service.NasConflict):remote.put_new('TFMK001/1.png',b'data')
    assert seen[0].headers['If-None-Match']=='*'
    assert seen[0].url.path.endswith('/TFMK001/1.png')
    remote.close()


def test_524_is_not_treated_as_upload_success():
    import httpx
    import pytest
    def transport(request):
        return httpx.Response(524 if request.method=='PUT' else 200,content=b'wrong',request=request)
    remote=service.WebDav.__new__(service.WebDav)
    remote.base='https://example.invalid/share'
    remote.client=httpx.Client(transport=httpx.MockTransport(transport))
    with pytest.raises(RuntimeError,match='524'):
        remote.put_new('temporary/1.png',b'data')
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


def test_preflight_rejects_invalid_or_wrong_panorama_dependency(client):
    import pytest
    b=client.post('/api/batches',json={'name':'preflight','enabled_series':['modern_business'],'target_archived_count':1,'designs_per_series':1,'standby_designs_per_series':0,'auto_start_panorama':False},headers={'X-User-Role':'leader'}).json()
    did=client.get(f"/api/designs/batch/{b['id']}").json()[0]['id']
    with get_db(immediate=True) as c:
        slots=c.execute('SELECT id,slot_type FROM slots WHERE design_id=?',(did,)).fetchall()
        for row in slots:
            vid='vid-'+row['slot_type']
            dep=None if row['slot_type']=='panorama' else 'vid-panorama'
            c.execute("INSERT INTO slot_versions(id,slot_id,design_id,prompt,process_status,is_locked,depends_on_panorama_version_id) VALUES (?,?,?,'x','completed',1,?)",(vid,row['id'],did,dep))
            c.execute("UPDATE slots SET status='approved',current_version_id=? WHERE id=?",(vid,row['id']))
        assert len(service.validate_upload_source(c,did))==4
        c.execute("UPDATE slot_versions SET depends_on_panorama_version_id='wrong' WHERE id='vid-twin_bed'")
        with pytest.raises(ValueError,match='全景版本不一致'):service.validate_upload_source(c,did)
        c.execute("UPDATE slot_versions SET depends_on_panorama_version_id='vid-panorama',is_invalidated=1 WHERE id='vid-twin_bed'")
        with pytest.raises(ValueError,match='状态不正确'):service.validate_upload_source(c,did)
