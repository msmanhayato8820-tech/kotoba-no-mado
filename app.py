"""Standard-library browser UI. Binds loopback only, no CDN, telemetry or access logs."""
import argparse
import json
import re
import os
import platform
import secrets
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from core import BASE, Engine, inspect_model, runtime_for, LANGUAGE_CATALOG

from model_catalog import DEFAULT_MODEL_ID, public_models
from model_setup import ModelSetupManager
from runtime_discovery import RuntimeDiscovery
import platform_support
from documents import extract, DocumentError, MAX_FILE_BYTES
from exporter import build_text, build_docx
from urllib.parse import unquote

MAX_BODY = 20000
# Larger bodies only for the paths that carry a whole document.
LARGE_BODY = {'/api/extract': MAX_FILE_BYTES, '/api/document/translate': 1_000_000, '/api/export': 6_000_000}


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_close(self):
        # Serialize final cleanup with in-flight start/stop requests, including
        # Control+C/launcher shutdown (which do not pass through /api/quit).
        with getattr(self, 'operation_lock', threading.RLock()):
            if hasattr(self, 'closing'):
                self.closing.set()
            try:
                if hasattr(self, 'runtime_discovery'):
                    self.runtime_discovery.close()
                if hasattr(self, 'setup'):
                    try:
                        self.setup.cancel()
                    finally:
                        self.setup.close()
            finally:
                try:
                    if hasattr(self, 'engine'):
                        self.engine.stop()
                finally:
                    super().server_close()

    def server_bind(self):
        # HTTPServer.server_bind() calls socket.getfqdn(), a name lookup that can
        # stall on some networks. Loopback needs no name, so skip it.
        if sys.platform == 'win32':
            # Windows SO_REUSEADDR permits a second process to hijack an active
            # listener. Require exclusive ownership instead, before bind().
            self.allow_reuse_address = False
            self.allow_reuse_port = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = '127.0.0.1', self.server_address[1]


PUBLIC_GET = ('/api/languages',)
GUIDE_CSP = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
# Third-party browser code (pdf.js). Only files listed with their hashes are served,
# and each is checked against the recorded SHA-256 before it is sent.
# The full new guide is Japanese; keep an honest, current English summary.
BEGINNER_GUIDE_EN = b"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kotoba no Mado 0.8 - First-run guide</title>
<style>body{font-family:system-ui,sans-serif;max-width:48rem;margin:2rem auto;padding:0 1rem;line-height:1.7}li{margin:1rem 0}a{color:#145b45}</style>
<main><h1>Kotoba no Mado 0.8 trial: first run</h1>
<p>The full 0.8 beginner guide is currently available only in Japanese. This page is a short English summary.</p>
<p><a href="/beginner/">Open the full Japanese 0.8 guide</a>. You can also open START-HERE-ja.html in the extracted app folder without starting Python.</p>
<ol>
<li>Install a standard 64-bit CPython 3.12, 3.13 or 3.14 if needed. The current instructions use Python 3.14, whose app compatibility is not yet verified. See the official <a href="https://www.python.org/downloads/windows/" target="_blank" rel="noopener noreferrer">Windows downloads</a> or <a href="https://www.python.org/downloads/macos/" target="_blank" rel="noopener noreferrer">macOS downloads</a>. Already have a supported version? You can skip installation.</li>
<li>Extract the entire ZIP. Keep its files together. Use Google Chrome.</li>
<li>Open Open-Windows.cmd on Windows or Open.command on Mac. Check the Python path and version shown. Keep the launcher window open while using the app.</li>
<li>Choose "Review 2B preparation". Read the download size, sources, save location, model terms and commercial-use registration requirements. Check the consent box yourself, then choose "Confirm and download". You can cancel and later review the plan to resume.</li>
<li>After verification, choose "Use this model". Review the separate terms checkbox, then choose "Load translation AI". Downloading alone does not start the AI.</li>
<li>Set the source to English and target to Japanese. Under "Translate a document", choose samples/English-to-Japanese.docx from the extracted folder. Check the three library paragraphs before translating. Start with the standard settings and a 4,096-token context.</li>
<li>Check the translation and its facts, then save it as Word (.docx) or text (.txt). Stop the AI or quit the app when finished.</li>
</ol>
<p>Initial Windows target: Windows 11 x64 and Chrome. Earlier 2B CPU tests used a Ryzen 5 8645HS with 16 GB and Python 3.12.14. Mac tests used a Mac mini M6 with 32 GB and Python 3.13.18. These are tested configurations, not minimum requirements or guarantees for other computers.</p>
<p>The new 0.8 installation-to-Word workflow is still under verification. Smartphones are not supported. GPU use is unverified. Independent Mac MLX 8B short-text tests are complete, but 8B integration into this app and Windows operation are unverified. 0.8B is unverified on Windows. If memory is tight, close other apps and use short text at 4,096 tokens.</p>
<p>Use the fictional sample first, not confidential or important documents. No external translation API is used. Preparation requires an internet connection.</p></main></html>"""

VENDOR_DIR = BASE / 'ui' / 'vendor' / 'pdfjs'
try:
    VENDOR = {item['path']: item for item in json.loads((VENDOR_DIR / 'MANIFEST.json').read_text(encoding='utf-8'))['files']}
except (OSError, ValueError, KeyError):
    VENDOR = {}
VENDOR_TYPES = {'.mjs': 'text/javascript; charset=utf-8', '.bcmap': 'application/octet-stream',
                '.pfb': 'application/octet-stream', '.ttf': 'font/ttf'}


def vendor_file(path):
    import hashlib
    item = VENDOR.get(path)
    if not item or Path(path).suffix not in VENDOR_TYPES:
        return None
    data = (VENDOR_DIR / item['path']).read_bytes()
    if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
        return None
    return data, VENDOR_TYPES[Path(path).suffix]


def create_server(port=0):
    engine = Engine()
    setup = ModelSetupManager()
    runtime_discovery = RuntimeDiscovery()
    operation_lock = threading.RLock()
    closing = threading.Event()

    def require_engine_available():
        if closing.is_set() or setup.status().get("active"):
            raise ValueError("2Bの準備中は翻訳AIを操作できません。準備が終わるか、中止してから試してください。")

    token = secrets.token_urlsafe(32)
    # One-time code handed only to the browser that this app opens (URL fragment,
    # never sent in an HTTP request line). It is exchanged for an HttpOnly cookie.
    launch = dict(code=secrets.token_urlsafe(24))
    sessions = set()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, code, data, mime='application/json; charset=utf-8', csp=None):
            body = json.dumps(data, ensure_ascii=False).encode() if isinstance(data, dict) else data
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', csp or "default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.send_header('Permissions-Policy', 'microphone=(self), camera=(), geolocation=()')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def redirect(self, location):
            self.send_response(302)
            self.send_header('Location', location)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def valid_host(self):
            return self.headers.get('Host') == '127.0.0.1:' + str(self.server.server_port)

        def cookie_name(self):
            return 'kotoba_session_' + str(self.server.server_port)

        def has_session(self):
            for part in (self.headers.get('Cookie') or '').split(';'):
                name, _, value = part.strip().partition('=')
                if name == self.cookie_name() and value and any(secrets.compare_digest(value, s) for s in sessions):
                    return True
            return False

        def do_GET(self):
            if not self.valid_host():
                return self.send(403, dict(error='このパソコンの画面から開いてください。'))
            if self.path.startswith('/api/') and self.path not in PUBLIC_GET and not self.has_session():
                return self.send(401, dict(error='このパソコンで起動したアプリから開いてください。', reopen=True))
            if self.path == '/api/models':
                return self.send(200, dict(default_model_id=DEFAULT_MODEL_ID, models=public_models(),
                    host=dict(zip(('platform', 'machine'), platform_support.host()))))
            if self.path == '/api/languages':
                return self.send(200, LANGUAGE_CATALOG)
            if self.path == '/api/status':
                return self.send(200, dict(app='local-translate-prototype', token=token, state=engine.status(),
                    privacy='文章・翻訳結果は保存しません。外部翻訳APIは使いません。', setup=setup.status(), runtime_cache=runtime_discovery.status()))
            if self.path in ('/api/setup/plan', '/api/setup/status'):
                try:
                    return self.send(200, setup.plan() if self.path.endswith('/plan') else setup.status())
                except ValueError as error:
                    return self.send(400, dict(error=str(error)))
                except Exception:
                    return self.send(500, dict(error='2Bの準備情報を確認できませんでした。アプリを起動し直してください。'))
            files = {'/': ('index.html', 'text/html; charset=utf-8'),
                     '/setup.js': ('setup.js', 'text/javascript; charset=utf-8'),
                     '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                     '/style.css': ('style.css', 'text/css; charset=utf-8'),
                     '/accessibility.css': ('accessibility.css', 'text/css; charset=utf-8'),
                     '/preferences.js': ('preferences.js', 'text/javascript; charset=utf-8'),
                     '/i18n.js': ('i18n.js', 'text/javascript; charset=utf-8'),
                     '/features.css': ('features.css', 'text/css; charset=utf-8'),
                     '/documents.js': ('documents.js', 'text/javascript; charset=utf-8'),
                     '/pdf-reader.js': ('pdf-reader.js', 'text/javascript; charset=utf-8'),
                     '/voice.js': ('voice.js', 'text/javascript; charset=utf-8')}
            # Never present pre-0.8 instructions or old screenshots as current.
            if self.path == '/beginner':
                return self.redirect('/beginner/')
            if self.path in ('/guide', '/beginner/', '/beginner/BEGINNER-GUIDE-ja.html'):
                return self.send(200, (BASE / 'START-HERE-ja.html').read_bytes(), 'text/html; charset=utf-8', GUIDE_CSP)
            if self.path in ('/guide?lang=en', '/beginner/BEGINNER-GUIDE-en.html'):
                return self.send(200, BEGINNER_GUIDE_EN, 'text/html; charset=utf-8', GUIDE_CSP)
            if self.path.startswith('/vendor/pdfjs/'):
                found = vendor_file(self.path[len('/vendor/pdfjs/'):])
                if found is None:
                    return self.send(404, dict(error='見つかりません。'))
                return self.send(200, found[0], found[1])
            if self.path not in files:
                return self.send(404, dict(error='見つかりません。'))
            name, mime = files[self.path]
            return self.send(200, (BASE / 'ui' / name).read_bytes(), mime)

        def start_session(self):
            try:
                length = int(self.headers.get('Content-Length', '0'))
                data = json.loads(self.rfile.read(length)) if 0 < length <= 200 else {}
            except ValueError:
                data = {}
            code = data.get('code') if isinstance(data, dict) else None
            if self.has_session():
                return self.send(200, dict(ok=True))
            if not (isinstance(code, str) and launch['code'] and secrets.compare_digest(code, launch['code'])):
                return self.send(401, dict(error='このパソコンで起動したアプリから開いてください。', reopen=True))
            launch['code'] = None  # single use
            session = secrets.token_urlsafe(32)
            sessions.add(session)
            body = json.dumps(dict(ok=True)).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Set-Cookie', '%s=%s; HttpOnly; SameSite=Strict; Path=/' % (self.cookie_name(), session))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            origin = 'http://127.0.0.1:' + str(self.server.server_port)
            if not self.valid_host() or self.headers.get('Origin') != origin:
                return self.send(403, dict(error='このパソコンのアプリ画面から操作してください。'))
            if self.path == '/api/session':
                return self.start_session()
            if not self.has_session():
                return self.send(401, dict(error='このパソコンで起動したアプリから開いてください。', reopen=True))
            if not secrets.compare_digest(self.headers.get('X-Local-Token') or '', token):
                return self.send(403, dict(error='このパソコンのアプリ画面から操作してください。'))
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if self.path == '/api/extract':
                    if not 0 < length <= MAX_FILE_BYTES or self.headers.get_content_type() != 'application/octet-stream':
                        raise ValueError('ファイルが大きすぎます（20 MBまで）。文書を分けてください。')
                    name = unquote(self.headers.get('X-File-Name', ''))[:255]
                    # Read into memory only; the file is never written to disk here.
                    return self.send(200, extract(name, self.rfile.read(length)))
                if not 0 < length <= LARGE_BODY.get(self.path, MAX_BODY) or self.headers.get_content_type() != 'application/json':
                    raise ValueError('入力サイズまたは形式が正しくありません。')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('入力形式が正しくありません。')
                if self.path == '/api/runtime/check':
                    if data:
                        raise ValueError('入力形式が正しくありません。')
                    with operation_lock:
                        require_engine_available()
                        return self.send(202, runtime_discovery.start())
                if self.path == '/api/setup/start':
                    # The client may acknowledge a displayed plan, never supply a URL/path.
                    if set(data) != {'acknowledged', 'plan_id'} or data.get('acknowledged') is not True or not isinstance(data.get('plan_id'), str) or not data['plan_id'] or len(data['plan_id']) > 128:
                        raise ValueError('取得する内容と利用条件を確認し、チェックを入れてください。')
                    with operation_lock:
                        if closing.is_set() or engine.status()['phase'] in ('loading', 'ready', 'translating'):
                            return self.send(409, dict(error='翻訳AIを止めてから、2Bの準備を始めてください。'))
                        return self.send(202, setup.start(acknowledged=True, plan_id=data['plan_id']))
                if self.path == '/api/setup/cancel':
                    if data:
                        raise ValueError('入力形式が正しくありません。')
                    with operation_lock:
                        return self.send(200, setup.cancel())
                if self.path == '/api/pick':
                    english = data.get('locale') == 'en'
                    if data.get('kind') == 'runtime':
                        prompt = 'Choose the llama.cpp folder' if english else 'llama.cppのフォルダを選択'
                    else:
                        prompt = 'Choose the folder with the downloaded PLaMo model' if english else '取得したPLaMoモデルのフォルダを選択'
                    try:
                        chosen = platform_support.pick_folder(prompt)
                    except subprocess.TimeoutExpired:
                        raise ValueError('フォルダ選択がタイムアウトしました。もう一度選ぶか、場所を入力してください。') from None
                    if not chosen:
                        return self.send(200, dict(cancelled=True))
                    return self.send(200, dict(path=chosen))
                if self.path == '/api/inspect':
                    return self.send(200, dict(model=inspect_model(data.get('path'), data.get('model_id', DEFAULT_MODEL_ID)), runtime=runtime_for(data.get('path'), data.get('runtime', ''))))
                if self.path == '/api/load':
                    with operation_lock:
                        require_engine_available()
                        engine.start(data.get('path'), data.get('runtime', ''), data.get('acknowledged'), data.get('model_id', DEFAULT_MODEL_ID), data.get('context_tokens', 4096),
                                     experimental_acknowledged=data.get('experimental_acknowledged'), background=True)
                    return self.send(202, dict(ok=True))
                if self.path == '/api/translate':
                    with operation_lock:
                        require_engine_available()
                        engine.translate(data.get('text'), data.get('source'), data.get('target'), data.get('limit', 256), data.get('kind', 'forward'))
                    return self.send(202, dict(ok=True))
                if self.path == '/api/document/translate':
                    with operation_lock:
                        require_engine_available()
                        count = engine.translate_document(data.get('text'), data.get('source'), data.get('target'), data.get('chunk', 600), data.get('context', 1), data.get('glossary'), data.get('name', ''))
                    return self.send(202, dict(ok=True, segments=count))
                if self.path == '/api/document/cancel':
                    engine.cancel_document()
                    return self.send(200, dict(ok=True))
                if self.path == '/api/document/result':
                    return self.send(200, engine.document_result())
                if self.path == '/api/document/clear':
                    engine.clear_document()
                    return self.send(200, dict(ok=True))
                if self.path == '/api/export':
                    kind = data.get('format')
                    if kind == 'txt':
                        return self.send(200, build_text(data.get('items'), data.get('layout')), 'text/plain; charset=utf-8')
                    if kind == 'docx':
                        return self.send(200, build_docx(data.get('items'), data.get('layout'), data.get('title') or '翻訳結果'),
                                         'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
                    raise ValueError('保存する形式を選び直してください。')
                if self.path == '/api/stop':
                    with operation_lock:
                        engine.stop()
                    return self.send(200, dict(ok=True))
                if self.path == '/api/clear':
                    engine.clear()
                    return self.send(200, dict(ok=True))
                if self.path == '/api/quit':
                    with operation_lock:
                        closing.set()
                        runtime_discovery.close()
                        setup.cancel()
                        engine.stop()
                    self.send(200, dict(ok=True))
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                return self.send(404, dict(error='見つかりません。'))
            except (ValueError, TypeError, OSError) as e:
                # User input validation contains no translated content.
                return self.send(400, dict(error=str(e) if isinstance(e, ValueError) else 'ファイルまたは入力を確認してください。'))
            except Exception:
                return self.send(500, dict(error='処理を続けられませんでした。モデルを停止し、もう一度試してください。'))
    server = LocalServer(('127.0.0.1', port), Handler)
    server.engine = engine
    server.setup = setup
    server.runtime_discovery = runtime_discovery
    server.closing = closing
    server.operation_lock = operation_lock
    server.launch = launch
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=50735)
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        server = create_server(args.port)
    except OSError:
        print('起動できません。このポートは使用中の可能性があります。先に起動したアプリを確認してください。', file=sys.stderr)
        print('Could not start: the port may already be in use. Check the earlier app instance. No other process was stopped.', file=sys.stderr)
        return 1
    url = 'http://127.0.0.1:' + str(server.server_port) + '/#code=' + server.launch['code']
    print('ことばの窓 0.8試用版: ' + url, flush=True)
    print('このアドレスは最初の1回だけ使えます。他の人に教えないでください。', flush=True)
    print('終了は画面の「アプリを終了」またはこのウィンドウで Control+C。', flush=True)
    if args.open:
        webbrowser.open(url)
    def stop_signal(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop_signal)
    signal.signal(signal.SIGINT, stop_signal)
    try:
        server.serve_forever()
    finally:
        server.server_close()

if __name__ == '__main__':
    sys.exit(main())
