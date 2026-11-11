"""Owned subprocess for GGUF models: runs the pinned llama.cpp `llama-server` on 127.0.0.1 only.

Same line protocol as worker.py, so token counting, the 80% budget, splitting, context
and glossary handling are shared (make_prompt / fit_prompt / count_tokens from worker.py).
Standard library only. No text is written to disk; llama-server logs are discarded.
"""
import http.client
import json
import os
import queue
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from owned_connection import OwnedHTTPConnection

OUT = sys.stdout
STOP_WORDS = ['<|plamo:tag|>', '<|plamo:bos|>']  # from the official GGUF model card


def emit(event, **values):
    OUT.write('@localtranslate:' + json.dumps(dict(event=event, **values), ensure_ascii=False) + '\n')
    OUT.flush()


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, exe, model, context_tokens):
        self.port = free_port()
        self.key = secrets.token_urlsafe(24)
        args = [exe, '-m', model, '-c', str(context_tokens), '--host', '127.0.0.1', '--port', str(self.port),
                '--jinja', '--no-prefill-assistant', '--offline', '--no-webui', '--no-slots', '--parallel', '1',
                '--alias', 'plamo-3-translate', '--log-disable']
        if os.environ.get('KOTOBA_LLAMA_CPU') == '1':
            args += ['-ngl', '0']  # development check: CPU only, like a laptop without a graphics card
        # No inherited proxy, and no inherited LLAMA_* setting: only the arguments above configure the server.
        # No inherited proxy and no inherited LLAMA_*/GGML_* setting. GGML_BACKEND_PATH
        # in particular would make ggml load a library from outside the checked folder.
        env = {k: v for k, v in os.environ.items() if not k.upper().endswith('_PROXY') and not k.upper().startswith(('LLAMA_', 'GGML_'))}
        # ggml also looks for backend libraries in the current folder. Make that the
        # same, fully checked runtime folder (unlisted libraries there are refused).
        folder = os.path.dirname(os.path.abspath(exe))
        env['LLAMA_API_KEY'] = self.key  # in the environment, not on the command line
        if sys.platform == 'win32':
            from windows_job import WindowsJobProcess
            self.proc = WindowsJobProcess.start(args, env, cwd=folder)
        else:
            self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         env=env, cwd=folder)

    def connection(self, timeout=600):
        connection = OwnedHTTPConnection(self.port, self.proc, timeout=timeout, ownership_timeout=min(1.0, timeout))
        connection.connect()  # no HTTP headers or body before OS ownership is proven
        return connection

    def alive(self):
        """Check the spawned process; connection() separately verifies TCP ownership."""
        if self.proc.poll() is not None:
            raise RuntimeError('llama-server ended')

    def post(self, path, body, timeout=600):
        self.alive()
        c = self.connection(timeout)
        try:
            c.request('POST', path, json.dumps(body), {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.key})
            r = c.getresponse()
            data = r.read()
            if r.status != 200:
                raise RuntimeError('llama-server %s %d' % (path, r.status))
            return json.loads(data)
        finally:
            c.close()

    def wait_ready(self, limit=240):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                return False
            c = None
            try:
                c = self.connection(min(2, max(.01, end - time.monotonic())))
                c.request('GET', '/health', headers={'Authorization': 'Bearer ' + self.key})
                if c.getresponse().status == 200:
                    return self.proc.poll() is None
            except (OSError, http.client.HTTPException):
                pass  # never fall back to an unchecked connection
            finally:
                if c is not None:
                    c.close()
            time.sleep(min(.5, max(0, end - time.monotonic())))
        return False

    def stop(self):
        if sys.platform == 'win32':
            # Closing the non-inherited job handle kills every descendant, even
            # when the main server already exited. OS handle cleanup also covers
            # a worker killed without running Python's finally blocks.
            self.proc.close()
            return
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass  # the server may have exited between poll and terminate
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
                self.proc.wait(timeout=8)

class Tokenizer:
    """The two calls worker.py needs, answered by llama-server with the template embedded in the GGUF."""
    def __init__(self, server):
        self.server = server

    def apply_chat_template(self, messages, target_lang, add_generation_prompt=True, tokenize=False, **options):
        kwargs = dict(target_lang=target_lang, **options)
        return self.server.post('/apply-template', dict(messages=messages, chat_template_kwargs=kwargs))['prompt']

    def encode(self, text, add_special_tokens=False):
        return self.server.post('/tokenize', dict(content=text, add_special=add_special_tokens, parse_special=True))['tokens']


def generate(server, tokens, limit, cancel):
    """Stream the translation; closing the connection makes llama-server stop at once."""
    body = dict(prompt=tokens, n_predict=limit, temperature=0, top_k=1, stream=True, cache_prompt=False, stop=STOP_WORDS)
    server.alive()
    c = server.connection()
    parts, stop_type = [], None
    try:
        c.request('POST', '/completion', json.dumps(body), {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + server.key})
        r = c.getresponse()
        if r.status != 200:
            raise RuntimeError('llama-server /completion %d' % r.status)
        for raw in r:
            if cancel.is_set():
                break
            line = raw.decode('utf-8', 'replace').strip()
            if not line.startswith('data:'):
                continue
            item = json.loads(line[5:])
            parts.append(item.get('content', ''))
            if item.get('stop'):
                stop_type = item.get('stop_type')
                break
    finally:
        c.close()
    return ''.join(parts).strip(), stop_type


def read_stdin(requests, cancel):
    try:
        for line in sys.stdin:
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if data.get('op') == 'cancel':
                cancel.set()  # stops the part being generated now
            else:
                requests.put(data)
    finally:
        if sys.platform == 'win32':
            # The app has gone away. Do not wait for loading or an HTTP call:
            # exiting closes our non-inherited kill-on-close job handle. Atomic
            # JOB_LIST assignment also protects a server being created now.
            os._exit(0)
        requests.put(None)  # macOS keeps the normal finally/server.stop path.


def main():
    model, model_id, context_tokens, exe = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
    from core import CONTEXT_TOKEN_CHOICES
    from worker import count_tokens, fit_prompt
    if context_tokens not in CONTEXT_TOKEN_CHOICES or not os.path.isfile(model) or not os.path.isfile(exe):
        emit('error', message='一度に扱える長さを一覧から選び直してください。')
        return
    if hasattr(signal, 'SIGTERM'):
        # Stopping the worker (app "Stop") must also stop llama-server.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    requests, cancel = queue.Queue(), threading.Event()
    reader = threading.Thread(target=read_stdin, args=(requests, cancel), daemon=True)
    if sys.platform == 'win32':
        reader.start()  # Watch the app's pipe before starting/loading the server.
    try:
        server = Server(exe, model, context_tokens)
    except OSError:
        emit('error', message='llama.cppを起動できませんでした。設定ガイドでフォルダを確認してください。')
        return
    try:
        if not server.wait_ready():
            emit('error', message='モデルを読み込めませんでした（llama.cpp）。対応版・空きメモリを確認してください。')
            return
        tokenizer = Tokenizer(server)
        if sys.platform != 'win32':
            reader.start()
        emit('ready')
        while True:
            data = requests.get()
            if data is None:
                return
            cancel.clear()
            started = time.perf_counter()
            try:
                if data.get('op') == 'count':
                    emit('counts', counts=count_tokens(tokenizer, data))
                    continue
                tokens, limit, used = fit_prompt(tokenizer, data, context_tokens)
                if tokens is None:
                    emit('input_error', code='too_long', message='一度に翻訳できる長さ（設定の8割）を超えています。文書として分けて翻訳します。')
                    continue
                text, stop_type = generate(server, tokens, limit, cancel)
                result = dict(text=text, seconds=round(time.perf_counter() - started, 2), context_used=used,
                              cancelled=cancel.is_set(),
                              warning='出力上限に達しました。文章を短くするか、出力上限を増やしてください。' if stop_type == 'limit' else None)
                emit('result', result=result)
            except Exception as e:
                emit('error', message='翻訳を停止しました（' + type(e).__name__ + '）。短い文章でモデルを読み込み直してください。')
                return
    finally:
        server.stop()


if __name__ == '__main__':
    main()
