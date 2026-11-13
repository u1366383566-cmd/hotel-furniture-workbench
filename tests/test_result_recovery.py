import io
import httpx
import pytest
from PIL import Image
from backend.app import image_adapter, watermark_service


def test_download_retries_same_url_without_reissuing_post():
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == 'POST':
            return httpx.Response(200, json={'data': [{'url': 'https://images.example/result?secret=hidden'}]})
        if calls.count('GET') < 3:
            return httpx.Response(503)
        return httpx.Response(200, content=b'image')
    transport = httpx.MockTransport(handler)
    original = httpx.Client
    class Client(original):
        def __init__(self, *args, **kwargs):
            super().__init__(transport=transport, **kwargs)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(image_adapter.httpx, 'Client', Client)
        data, _ = image_adapter.generate_image_real({'enabled': True, 'base_url':'https://api.example', 'model':'m', 'api_key':'secret'}, 'prompt')
    assert data == b'image'
    assert calls == ['POST', 'GET', 'GET', 'GET']


def test_preview_recovery_reuses_saved_raw(tmp_path, monkeypatch):
    monkeypatch.setattr(watermark_service, 'RAW_IMAGES_DIR', tmp_path)
    monkeypatch.setattr(watermark_service, 'PREVIEW_IMAGES_DIR', tmp_path)
    raw = io.BytesIO()
    Image.new('RGB', (10, 10), 'red').save(raw, format='PNG')
    attempts = []
    original = watermark_service.apply_watermark
    def flaky(img, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise ValueError('temporary preview failure')
        return original(img, **kwargs)
    monkeypatch.setattr(watermark_service, 'apply_watermark', flaky)
    raw_name, preview_name = watermark_service.save_real_slot_image(raw.getvalue(), 'panorama', 's', 'code', 1)
    assert len(attempts) == 3
    assert (tmp_path / raw_name).exists() and (tmp_path / preview_name).exists()


def test_failed_download_does_not_expose_signed_url():
    secret_url = 'https://images.example/result?token=secret-value'
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == 'POST':
            return httpx.Response(200, json={'data': [{'url': secret_url}]})
        raise httpx.ReadTimeout(f'failed at {secret_url}', request=request)
    original = httpx.Client
    transport = httpx.MockTransport(handler)
    class Client(original):
        def __init__(self, *args, **kwargs):
            super().__init__(transport=transport, **kwargs)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(image_adapter.httpx, 'Client', Client)
        with pytest.raises(image_adapter.ImageResultDownloadError) as raised:
            image_adapter.generate_image_real({'enabled': True, 'base_url':'https://api.example',
                                               'model':'m', 'api_key':'secret'}, 'prompt')
    assert 'secret-value' not in str(raised.value)
    assert calls == ['POST', 'GET', 'GET', 'GET']
