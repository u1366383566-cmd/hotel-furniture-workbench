"""Browser acceptance with an isolated database and a local OpenAI-compatible planner."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent


class Planner(BaseHTTPRequestHandler):
    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        context = json.loads(data['messages'][1]['content'])
        index = len(context['existing_designs']) + 1
        spec = {'headboard': f'第{index}款横向木饰面床屏', 'bed_frame':'悬浮床体', 'nightstand':'木质抽屉柜',
                'wardrobe':'开放式衣柜', 'main_materials':['胡桃木'], 'color_scheme':['暖灰'],
                'companion_items':['布艺沙发'], 'soft_furnishings':'米白床品', 'design_difference':f'第{index}款分段床屏',
                'panorama_prompt':f'酒店客房，第{index}款木饰面家具，暖灰床品。'}
        payload = json.dumps({'choices':[{'message':{'content':json.dumps(spec, ensure_ascii=False)}}]}, ensure_ascii=False).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)

    def log_message(self, *_):
        pass


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); return s.getsockname()[1]


def main():
    planner = ThreadingHTTPServer(('127.0.0.1', 0), Planner)
    threading.Thread(target=planner.serve_forever, daemon=True).start()
    port = free_port()
    with tempfile.TemporaryDirectory(prefix='workbench-smart-') as storage:
        env = dict(os.environ, WORKBENCH_STORAGE_DIR=storage)
        env.pop('WORKBENCH_DB_PATH', None)
        server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'backend.app.main:app', '--host', '127.0.0.1', '--port', str(port)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=.5); break
                except Exception:
                    time.sleep(.1)
            else:
                raise RuntimeError('server did not start')
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel='msedge', headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 900})
                errors = []
                page.on('pageerror', lambda exc: errors.append(exc.stack or str(exc)))
                page.on('dialog', lambda dialog: dialog.accept())
                page.goto(f'http://127.0.0.1:{port}/', wait_until='networkidle')
                page.locator('.role-btn[data-role="leader"]').click()
                with page.expect_response(lambda r: '/api/system/text_api' in r.url and r.request.method == 'GET'):
                    page.locator('[data-tab="system"]').click()
                assert not errors, errors
                page.locator('#cfg-text-url').fill(f'http://127.0.0.1:{planner.server_port}')
                page.locator('#cfg-text-model').fill('fake-planner')
                page.locator('#cfg-text-key').fill('test')
                with page.expect_response(lambda r: '/api/system/text_api' in r.url and r.request.method == 'PUT') as saved:
                    page.locator('#btn-text-save').click()
                assert saved.value.status == 200, saved.value.text()
                page.locator('[data-tab="panorama"]').click()
                page.locator('#btn-new-batch').click()
                page.locator('#new-batch-name').fill('浏览器智能规划')
                page.locator('#new-batch-target').fill('2')
                page.locator('#new-batch-warning').fill('20')
                page.locator('#new-batch-hardlimit').fill('25')
                for sid in ('natural_wood', 'soft_contemporary'):
                    page.locator(f'.series-check[value="{sid}"]').uncheck()
                page.locator('#new-batch-brief').fill('温暖商务')
                page.locator('#btn-submit-new-batch').click()
                page.locator('#smart-planning-panel').wait_for(state='visible')
                assert page.locator('#planning-cards article').count() == 3
                page.locator('#btn-plan-all').click()
                page.get_by_text('已规划 3 / 3 款（包含候补）').wait_for(timeout=30000)
                if os.environ.get('SMART_PLANNING_SCREENSHOT'):
                    page.locator('#smart-planning-panel').screenshot(path=os.environ['SMART_PLANNING_SCREENSHOT'])
                assert page.get_by_text('可能相似的设计').count() == 3
                assert page.locator('#btn-start-batch').is_hidden()
                first_card = page.locator('#planning-cards article').first
                assert not first_card.locator('textarea').first.is_visible()
                assert page.get_by_text('查看 / 编辑').count() == 3
                first_card.locator('.plan-summary').click()
                assert first_card.locator('textarea').first.is_visible()
                first_card.locator('textarea').first.fill('人工调整的床屏')
                page.locator('#btn-plan-confirm').click()
                page.get_by_text('请先保存或重新规划尚未保存的修改，再确认方案').wait_for()
                first_card.locator('input[type="checkbox"]').check()
                first_card.get_by_text('保存本款修改').click()
                page.get_by_text('方案已保存').wait_for()
                page.locator('#btn-plan-confirm').click()
                page.locator('#btn-start-batch').wait_for(state='visible')
                page.locator('#plan-start-hint').wait_for(state='visible')
                page.get_by_text('已确认的设计方案').wait_for()
                assert page.get_by_text('尚未启动全景生成').count() == 2
                page.locator('#btn-start-from-plans').click()
                page.locator('[data-action="pano-compare"]').first.wait_for(timeout=30000)
                page.locator('[data-action="pano-compare"]').first.click()
                page.get_by_text('历史款对照与成图特征核实').wait_for()
                page.get_by_text('保存核实特征').click()
                page.get_by_text('已保存核实特征').wait_for()
                page.locator('[data-tab="standby"]').click()
                page.locator('#btn-append-standby').click()
                page.locator('#smart-planning-panel').wait_for(state='visible')
                page.get_by_text('已规划 0 / 1 款（包含候补）').wait_for()
                page.locator('#btn-plan-all').click()
                page.get_by_text('已规划 1 / 1 款（包含候补）').wait_for(timeout=30000)
                page.get_by_text('确认追加候补').click()
                page.locator('#smart-planning-panel').wait_for(state='hidden')
                assert not errors, errors
                browser.close()
        finally:
            server.terminate(); server.wait(timeout=15); planner.shutdown(); planner.server_close()
    print('smart planning browser acceptance passed')


if __name__ == '__main__':
    main()
