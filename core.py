"""Local-only PLaMo model validation and owned worker lifecycle. No third-party imports."""
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from model_catalog import DEFAULT_MODEL_ID, model_entry, manifest_for, is_experimental, experimental_enabled
import platform_support
from documents import split_segments, joiner, MAX_TEXT_CHARS

BASE = Path(__file__).resolve().parent
MANIFEST = json.loads((BASE / 'model-manifest.json').read_text(encoding='utf-8'))
MODEL_NAME = 'PLaMo 3 Translate 31B MLX-4bit'
LANGUAGE_CATALOG = json.loads((BASE / 'languages.json').read_text(encoding='utf-8'))
LANGUAGE_CODES = frozenset(item['code'] for item in LANGUAGE_CATALOG['languages'])


AUTO = 'auto'  # let the model detect the source language (source_lang omitted)
EVALUATION = LANGUAGE_CATALOG.get('official_evaluation', {})


def validate_language_pair(source, target, allow_auto=True):
    """Any two different supported languages. Japanese is no longer required on one side."""
    if not isinstance(source, str) or not isinstance(target, str) or target not in LANGUAGE_CODES:
        raise ValueError('対応する言語を一覧から選んでください。')
    if source == AUTO:
        if not allow_auto:
            raise ValueError('元の言語に戻すには、翻訳元の言語を指定してください。')
        return source, target
    if source not in LANGUAGE_CODES:
        raise ValueError('対応する言語を一覧から選んでください。')
    if source == target:
        raise ValueError('翻訳元と翻訳先に、別の言語を選んでください。')
    return source, target


def pair_status(source, target):
    """'official' when the pair is in the model card's published evaluation, else 'unevaluated'."""
    hubs, scope = set(EVALUATION.get('hubs', [])), set(EVALUATION.get('languages', []))
    if source == AUTO:
        return 'unknown'
    if (source in hubs and target in scope) or (target in hubs and source in scope):
        return 'official'
    return 'unevaluated'


def clean_path(value):
    """Trim spaces and one pair of surrounding quotes (Windows "Copy as path" adds them)."""
    if not isinstance(value, str):
        return value
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
        value = value[1:-1].strip()
    return value


def inspect_model(value, model_id=DEFAULT_MODEL_ID, *, cancelled=None, hash_files=True):
    """hash_files=False is only the quick pre-check (folder, names, sizes) made before a
    cancellable background load; that load always repeats the full check."""
    entry = model_entry(model_id)
    manifest = MANIFEST if model_id == DEFAULT_MODEL_ID else manifest_for(model_id)
    if not isinstance(value, str) or not value.strip():
        raise ValueError('モデルフォルダを選んでください。')
    value = clean_path(value)
    if not value:
        raise ValueError('モデルフォルダを選んでください。')
    root = Path(value).expanduser().resolve()
    if entry['backend'] == 'llama.cpp' and root.is_file():
        if root.name not in [item['name'] for item in manifest['files'] if item['name'].endswith('.gguf')]:
            raise ValueError('選んだGGUFファイルは対応する固定版のファイル名と異なります。')
        root = root.parent  # the GGUF file itself was chosen
    if not root.is_dir():
        raise ValueError('フォルダが見つかりません。GGUFファイルではなく、PLaMo MLXのフォルダを選んでください。' if entry['backend'] == 'mlx'
                         else 'フォルダが見つかりません。GGUFファイルを保存したフォルダを選んでください。')
    for item in manifest['files']:
        if cancelled and cancelled():
            raise ValueError('モデルの検査を中止しました。')
        p = root / item['name']
        if not p.is_file() or p.stat().st_size != item['bytes']:
            raise ValueError('モデルのファイルが不足、または対応版と異なります: ' + item['name'])
        # All code, tokenizer, template and config are pinned. Large weights: size only.
        if hash_files and (not item['name'].endswith('.safetensors') or manifest.get('verify_weight_hashes')):
            digest = hashlib.sha256()
            with p.open('rb') as stream:
                while True:
                    if cancelled and cancelled():
                        raise ValueError('モデルの検査を中止しました。')
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            if digest.hexdigest() != item['sha256']:
                raise ValueError('安全のため読み込みません。対応版と内容が異なります: ' + item['name'])
    gguf = [x['name'] for x in manifest['files'] if x['name'].endswith('.gguf')]
    return dict(path=str(root), name=entry['name'], model_id=model_id, backend=entry['backend'],
                model_file=str(root / gguf[0]) if gguf else None,
                bytes=sum(x['bytes'] for x in manifest['files']),
                revision=manifest['revision'], weight_integrity='sha256' if manifest.get('verify_weight_hashes') else 'size-only',
                allocation_cap_bytes=entry.get('allocation_cap_bytes'),
                context_token_cap=entry['context_token_cap'])


def _own_environment(python):
    """An auto-found venv is used only when its folders and link belong to this account."""
    if not hasattr(os, 'getuid'):
        return False  # MLX is Mac-only; never auto-pick elsewhere.
    try:
        return all(item.lstat().st_uid == os.getuid() for item in (python, python.parent, python.parent.parent))
    except OSError:
        return False


def runtime_for(model_path, requested=''):
    explicit = isinstance(requested, str) and bool(requested.strip())
    requested = clean_path(requested) if explicit else ''
    if explicit:
        # Even '""' is an explicit entry: it must fail, never fall back to a search.
        candidates = [Path(requested).expanduser()] if requested else []
    else:
        # Unlike llama.cpp, a Python environment cannot be pinned by hash. Look
        # only next to the chosen model and in this app folder, never in a fixed
        # home-folder name, and only at environments this account created.
        candidates = [Path(model_path).parent / '.venv/bin/python',
                      BASE / '.venv/bin/python', Path(sys.executable)]
    for p in candidates:
        # Do not resolve the venv symlink: Python needs the venv location.
        if p.is_file() and os.access(str(p), os.X_OK):
            if not explicit and p != Path(sys.executable) and not _own_environment(p):
                continue
            return str(p.absolute())
    raise ValueError('Python実行環境が見つかりません。設定ガイドを開いてください。')


def memory_snapshot():
    return platform_support.memory_snapshot()


LLAMA_MANIFEST = BASE / 'manifests' / 'llama-cpp.json'


def _signal_group(p, sig):
    """macOS: the worker leads its own process group, so llama-server under it stops too."""
    if os.name == 'posix' and isinstance(getattr(p, 'pid', None), int):
        try:
            os.killpg(p.pid, sig)
            return True
        except OSError:
            pass
    return False


_POPEN = subprocess.Popen  # the real class, even while tests replace subprocess.Popen


def sweep_group(p, grace=3.0):
    """POSIX: end whatever is left in a worker's process group after the worker is gone.

    llama-server shares the worker's group. If the worker crashed or was killed, it
    could not stop the server itself. A group ID is not reused while any member is
    alive, so signalling it reaches only processes this app started. Only real
    processes we launched are swept, never test doubles. POSIX group IDs do not
    carry generation tokens, so this is not protection against arbitrary local
    process manipulation or the last-member-exit/group-ID-reuse race.
    """
    if os.name != 'posix' or not isinstance(p, _POPEN) or not isinstance(p.pid, int):
        return
    for sig, wait in ((signal.SIGTERM, grace), (getattr(signal, 'SIGKILL', signal.SIGTERM), 1.0)):
        try:
            os.killpg(p.pid, sig)
        except OSError:
            return  # the group is already empty
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                os.killpg(p.pid, 0)
            except OSError:
                return
            time.sleep(0.05)


def terminate_worker(p):
    if not _signal_group(p, signal.SIGTERM):
        p.terminate()


def kill_worker(p):
    # Windows: llama-server is in a job object owned by the worker, so it ends with the worker.
    if not _signal_group(p, getattr(signal, 'SIGKILL', signal.SIGTERM)):
        p.kill()


NETWORK_RUNTIME = ('ネットワーク上の場所にある実行プログラムは、安全のため読み込みません。'
                   'このパソコンの中（ローカルのドライブ）へフォルダごとコピーしてから指定してください。')


def manual_runtime_folder(folder, exe, plat):
    """Policy for a typed runtime folder: links stay allowed, network locations do not.

    A link is followed once, here, and the real folder is what is hashed, used as the
    working folder and launched, so re-pointing the link after the check has no effect.
    Programs on a network share can be replaced by others between the check and the
    launch, so they are refused. The model file (data, fully hashed) is not affected.
    If the kind of location cannot be determined, the folder is accepted as before.
    """
    if plat == 'win32' and str(folder).startswith(('\\\\', '//')):
        raise ValueError(NETWORK_RUNTIME)
    try:
        if folder.is_file() and folder.name == exe:
            # A link to llama-server itself: check the folder it really lives in.
            real = folder.resolve(strict=True).parent
        else:
            real = folder.resolve(strict=True)
    except (OSError, RuntimeError):
        return folder  # missing or unreadable: the usual "not found" answer follows
    if (plat == 'win32' and str(real).startswith(('\\\\', '//'))) or platform_support.network_location(real):
        raise ValueError(NETWORK_RUNTIME)
    return real


def llama_runtime(requested='', *, cancelled=None):
    """Find llama-server from the pinned llama.cpp release and check every file before use.

    All executables and libraries listed for this OS must match their SHA-256, symlinks must
    point to listed files in the same folder, and no unlisted library may sit next to them.
    """
    try:
        manifest = json.loads(LLAMA_MANIFEST.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ValueError('llama.cppの検査情報がありません。安全のため読み込みません。') from None
    plat, machine = platform_support.host()
    pinned = manifest.get('binaries', {}).get('%s-%s' % (plat, machine))
    if not pinned:
        raise ValueError('このパソコンのOS用のllama.cppは、この版ではまだ用意していません。')
    exe = pinned.get('exe', 'llama-server.exe' if plat == 'win32' else 'llama-server')
    release = manifest['release']
    if not isinstance(requested, str):
        raise ValueError('実行プログラムのフォルダを入力してください。')
    # A supplied path always wins, even if it is invalid. Never search arbitrary
    # folders or fall back from a bad explicit path to a working cache.
    automatic = not requested.strip()
    if automatic:
        from runtime_discovery import prepared_runtime_location, check_cache_layout
        try:
            folder, spec = prepared_runtime_location()
            check_cache_layout(folder, spec)
        except FileNotFoundError:
            raise ValueError('2B準備の実行プログラムが見つかりません。実行部フォルダを指定してください。') from None
        folders = [folder]
    else:
        cleaned = clean_path(requested)
        if not cleaned:
            raise ValueError('実行プログラムのフォルダを入力してください。')
        folders = [manual_runtime_folder(Path(cleaned).expanduser(), exe, plat)]
    for folder in folders:
        if folder.is_file() and folder.name == exe:
            folder = folder.parent
        if not (folder / exe).is_file():
            continue
        listed = pinned.get('files') or {exe: dict(sha256=pinned['sha256'])}
        for name, item in listed.items():
            f = folder / name
            if cancelled and cancelled():
                raise ValueError('実行プログラムの検査を中止しました。')
            valid = f.is_file() and ('bytes' not in item or f.stat().st_size == item['bytes'])
            digest = hashlib.sha256()
            if valid:
                with f.open('rb') as stream:
                    while True:
                        if cancelled and cancelled():
                            raise ValueError('実行プログラムの検査を中止しました。')
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
            if not valid or digest.hexdigest() != item['sha256']:
                raise ValueError('安全のため読み込みません。llama.cppが対応版（%s）と異なります: %s' % (release, name))
        for f in folder.iterdir():
            library = f.suffix.lower() in ('.dll', '.dylib', '.so', '.exe') or '.dylib' in f.name
            if f.is_symlink():
                target = f.resolve()
                if target.parent != folder.resolve() or target.name not in listed:
                    raise ValueError('安全のため読み込みません。llama.cppのフォルダに対応版以外のファイルがあります: ' + f.name)
            elif library and f.name not in listed:
                raise ValueError('安全のため読み込みません。llama.cppのフォルダに対応版以外のファイルがあります: ' + f.name)
        return str((folder / exe).resolve())
    raise ValueError('llama.cpp（%s）のフォルダが見つかりません。設定ガイドの手順で用意し、「詳しい設定」で場所を指定してください。' % release)


def private_env():
    env = dict(os.environ)
    # Never inherit a proxy, remote endpoint or Python injection override.
    for k in list(env):
        # LLAMA_* would silently reconfigure llama-server; the loader variables inject code.
        if (k.upper().endswith('_PROXY') or k in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'LD_PRELOAD', 'DYLD_INSERT_LIBRARIES')
                or k.upper().startswith(('LLAMA_', 'GGML_'))):
            env.pop(k, None)
    cache = BASE / '.runtime'
    cache.mkdir(mode=0o700, exist_ok=True)
    env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1', HF_HUB_DISABLE_IMPLICIT_TOKEN='1', MLX_ENABLE_TF32='0',
               DO_NOT_TRACK='1', PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8', TOKENIZERS_PARALLELISM='false',
               HF_HOME=str(cache / 'huggingface'), HF_MODULES_CACHE=str(cache / 'modules'),
               NUMBA_CACHE_DIR=str(cache / 'numba'))
    return env


# 32,768 = official maximum used in training (input + output). Higher is not offered.
CONTEXT_TOKEN_CHOICES = (4096, 8192, 16384, 32768)


class Engine:
    def __init__(self):
        self.lock = threading.RLock()
        self.proc = None
        self._cleanups = set()
        self.serial = 0
        self.messages = queue.Queue()
        self.state = dict(phase='idle', message='モデルを選んで始めましょう。', result=None,
                          model=None, seconds=None, error=None, memory=None, job=None,
                          forward=None, reverse=None, pending=None, clear_serial=0, long_input=None)
        self.job_segments = []
        self.job_cancel = False

    def status(self):
        retired = None
        with self.lock:
            if self.proc is not None and self.proc.poll() is not None and self.state['phase'] in ('ready', 'loading', 'translating'):
                waiting = self.state['phase'] != 'ready'
                self.state.update(phase='error', error='モデルの処理が終了しました。メモリや実行環境を確認して、もう一度読み込んでください。')
                if not waiting:
                    # Nothing is waiting on a worker that died while idle, so no
                    # _fail() will release it. Forget the ended process here;
                    # otherwise Stop is disabled and Load is refused for good.
                    p, self.proc = self.proc, None
                    self.serial += 1
                    self.state['pending'] = None
                    # The worker crashed: its llama-server may still be running.
                    done = self._track_cleanup()
                    retired = (p, done)
                    self._end_job('error')
                    for stream in (getattr(p, 'stdin', None), getattr(p, 'stdout', None)):
                        try:
                            stream.close()
                        except Exception:
                            pass
            state = dict(self.state)
        if retired is not None:
            try:
                threading.Thread(target=self._sweep_retired, args=retired, daemon=False).start()
            except Exception:
                # Resource exhaustion must not leave an unset cleanup event
                # hanging Stop/Quit forever. Bounded fallback, outside the lock.
                self._sweep_retired(*retired)
        return state

    def _track_cleanup(self):
        # Caller holds self.lock, before releasing the old process handle.
        done = threading.Event()
        self._cleanups.add(done)
        return done

    def _finish_cleanup(self, done):
        done.set()
        with self.lock:
            self._cleanups.discard(done)

    def _wait_cleanups(self):
        # Never hold the engine lock while a previous worker is being reclaimed.
        with self.lock:
            pending = tuple(self._cleanups)
        for done in pending:
            done.wait()

    def _sweep_retired(self, p, done):
        try:
            sweep_group(p)
        finally:
            self._finish_cleanup(done)

    def stop(self):
        with self.lock:
            self.serial += 1
            p, self.proc = self.proc, None
            done = self._track_cleanup() if p is not None else None
            self.state.update(phase='idle', message='モデルを停止しました。メモリを解放しています。', result=None, error=None,
                              forward=None, reverse=None, pending=None)
            self._end_job('cancelled')
        if p is not None:
            try:
                try:
                    terminate_worker(p)
                    p.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    kill_worker(p)
                    p.wait(timeout=3)
                sweep_group(p)
            finally:
                for stream in (p.stdin, p.stdout):
                    try:
                        stream.close()
                    except Exception:
                        pass
                self._finish_cleanup(done)
        # status() may already have released the dead worker. Stop/quit must
        # still await its owned descendants, not abandon a daemon reaper.
        self._wait_cleanups()

    def start(self, path, runtime='', acknowledged=False, model_id=DEFAULT_MODEL_ID, context_tokens=4096, *, experimental_acknowledged=False, background=False):
        if acknowledged is not True:
            raise ValueError('モデルを自分で取得し、利用条件を確認したことをチェックしてください。')
        entry = model_entry(model_id)
        if is_experimental(entry):
            if not experimental_enabled(entry):
                raise ValueError('この未検証の試用はWindows x64専用です。このパソコンでは読み込めません。')
            if experimental_acknowledged is not True:
                raise ValueError('実機未検証・CPU実行・必要メモリと速度の保証がないことを確認し、未検証で試すチェックを入れてください。')
        if type(context_tokens) is not int or context_tokens not in CONTEXT_TOKEN_CHOICES or context_tokens > entry.get('max_context_tokens', 4096):
            raise ValueError('一度に扱える長さを一覧から選び直してください。')
        if is_experimental(entry):
            return self._start_experimental(path, runtime, entry, context_tokens)
        if background:
            # The app screen: report a wrong folder or size at once, then hash and
            # launch in the background so Stop and Quit work during the check.
            inspect_model(path, model_id, hash_files=False)
            info = dict(model_id=entry['id'], name=entry['name'], backend=entry['backend'], context_token_cap=context_tokens)
            return self._reserve(path, runtime, entry, context_tokens, info, self._prepare_standard,
                                 'ファイルをSHA-256で検査しています。大きなモデルでは時間がかかります。「モデルを停止」で中断できます。')
        info = inspect_model(path, model_id)
        info['context_token_cap'] = context_tokens
        return self._launch(info, runtime, entry, context_tokens)

    def _start_experimental(self, path, runtime, entry, context_tokens):
        info = dict(model_id=entry['id'], name=entry['name'], backend=entry['backend'],
                    context_token_cap=context_tokens, experimental_unverified=True)
        return self._reserve(path, runtime, entry, context_tokens, info, self._prepare_experimental,
                             '固定版のファイルをSHA-256で検査中です。大容量では時間がかかります。停止できます。')

    def _reserve(self, path, runtime, entry, context_tokens, info, target, message):
        if not isinstance(path, str) or not path.strip():
            raise ValueError('モデルフォルダを選んでください。')
        # Reserve before returning HTTP 202. No large hashing or runtime launch
        # occurs while the HTTP operation lock is held. Stop invalidates serial,
        # and _launch re-checks it under the lock right before starting anything.
        with self.lock:
            if self.state['phase'] in ('loading', 'translating') or self.proc is not None:
                raise ValueError('処理中または読み込み済みです。停止してから切り替えてください。')
            self.serial += 1
            serial = self.serial
            self.state.update(phase='loading', message=message,
                              model=info, result=None, error=None, seconds=None, forward=None, reverse=None, pending=None, job=None, memory=None)
            thread = threading.Thread(target=target,
                                      args=(path, runtime, entry, context_tokens, serial), daemon=True)
            try:
                thread.start()
            except Exception:
                self.state.update(phase='error', error='モデルの検査を開始できませんでした。もう一度読み込んでください。')
                raise ValueError(self.state['error']) from None
        return info

    def _prepare_experimental(self, path, runtime, entry, context_tokens, serial):
        self._prepare_reserved(path, runtime, entry, context_tokens, serial, dict(experimental_unverified=True))

    def _prepare_standard(self, path, runtime, entry, context_tokens, serial):
        self._prepare_reserved(path, runtime, entry, context_tokens, serial, {})

    def _prepare_reserved(self, path, runtime, entry, context_tokens, serial, extra):
        try:
            info = inspect_model(path, entry['id'], cancelled=lambda: serial != self.serial)
            info.update(context_token_cap=context_tokens, **extra)
            if serial == self.serial:
                self._launch(info, runtime, entry, context_tokens, reserved_serial=serial)
        except Exception as error:
            with self.lock:
                if serial == self.serial:
                    self.state.update(phase='error', error=str(error))

    def _launch(self, info, runtime, entry, context_tokens, reserved_serial=None):
        self._wait_cleanups()  # do not overlap a new model with a retiring server
        model_id = entry['id']
        plat, machine = platform_support.host()
        if [plat, machine] not in entry.get('hosts', []):
            raise ValueError('この翻訳AIは、このパソコンのOSでは使えません。' if entry['backend'] == 'llama.cpp' else 'この試作のPLaMo MLX実行はApple Silicon Mac専用です。')
        if entry['backend'] == 'llama.cpp':
            server = llama_runtime(runtime, cancelled=lambda: reserved_serial != self.serial) if reserved_serial is not None else llama_runtime(runtime)
            command = [sys.executable, '-u', str(BASE / 'worker_gguf.py'), info['model_file'], model_id, str(context_tokens), server]
        else:
            python = runtime_for(info['path'], runtime)
            command = [python, '-u', str(BASE / 'worker.py'), info['path'], model_id, str(context_tokens)]
        snapshot = memory_snapshot()
        if snapshot is None:
            raise ValueError('メモリ状態を確認できません。安全のため読み込みを開始しません。')
        if snapshot['free'] < 20:
            raise ValueError('メモリの余裕が少ないため開始できません。他の大きなアプリを閉じてください。')
        with self.lock:
            if reserved_serial is not None and reserved_serial != self.serial:
                return None  # Stop/quit or a newer load won while verification ran.
            if reserved_serial is None and self.state['phase'] in ('loading', 'translating'):
                raise ValueError('処理中です。停止してから切り替えてください。')
            if self.proc is not None:
                raise ValueError('別のモデルが読み込み済みです。先に「モデルを停止」を押してください。')
            if reserved_serial is None:
                self.serial += 1
            serial = self.serial
            q = queue.Queue()
            self.messages = q
            self.state.update(phase='loading', message='PLaMoを準備中です。初回は数分かかることがあります。',
                              model=info, result=None, error=None, seconds=None, forward=None, reverse=None, pending=None, job=None, memory=dict(baseline=snapshot, latest=snapshot, peak_swap_growth_mb=0, min_free_percentage=snapshot['free']))
            try:
                group = dict(start_new_session=True) if os.name == 'posix' else dict(creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
                env = private_env()
                if entry['backend'] == 'llama.cpp' and plat == 'win32':
                    env['KOTOBA_LLAMA_CPU'] = '1'  # pinned Windows CPU build; never request GPU offload
                p = subprocess.Popen(command, **group,
                                     cwd=str(BASE), env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, encoding='utf-8', bufsize=1)
            except OSError:
                self.state.update(phase='error', error='Pythonを起動できません。実行環境のパスを確認してください。')
                raise ValueError(self.state['error']) from None
            self.proc = p
            threading.Thread(target=self._reader, args=(p, q), daemon=True).start()
            threading.Thread(target=self._wait_load, args=(p, q, serial, snapshot), daemon=True).start()
            threading.Thread(target=self._guard, args=(p, serial, snapshot), daemon=True).start()
        return info

    @staticmethod
    def _reader(p, q):
        try:
            for line in p.stdout:
                if line.startswith('@localtranslate:'):
                    try:
                        q.put(json.loads(line[len('@localtranslate:'):]))
                    except ValueError:
                        pass
        finally:
            q.put(dict(event='error', message='実行環境を開始できませんでした。MLX、mlx-lm、Transformers、Numbaの導入状態をガイドで確認してください。'))

    def _next(self, p, q, serial, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if serial != self.serial:
                return None
            try:
                return q.get(timeout=.25)
            except queue.Empty:
                if p.poll() is not None:
                    continue
        raise ValueError('待ち時間の上限を超えました。原文を短くするか、実行環境を確認してください。')

    def _fail(self, p, serial, message):
        with self.lock:
            if serial != self.serial:
                return
            self.serial += 1
            done = self._track_cleanup()
            self.proc = None
            self.state.update(phase='error', error=message, message='安全のため処理を停止しました。設定やメモリを確認してください。', result=None, pending=None)
            self._end_job('error')
        try:
            if p.poll() is None:
                terminate_worker(p)
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                kill_worker(p)
                p.wait(timeout=3)
            sweep_group(p)  # the worker may have died first, leaving llama-server behind
        finally:
            self._finish_cleanup(done)

    def _wait_load(self, p, q, serial, baseline):
        started = time.monotonic()
        try:
            event = self._next(p, q, serial, 240)
            if event is None:
                return
            if event.get('event') != 'ready':
                raise ValueError(event.get('message', 'モデルを読み込めません。'))
            with self.lock:
                if serial == self.serial:
                    self.state.update(phase='ready', message='準備できました。文章を入力してください。', seconds=round(time.monotonic() - started, 2))
        except Exception as e:
            self._fail(p, serial, str(e))

    def _guard(self, p, serial, baseline):
        while p.poll() is None and serial == self.serial:
            time.sleep(1)
            current = memory_snapshot()
            if current is None:
                self._fail(p, serial, 'メモリの監視ができなくなったため、安全のためモデルを停止しました。実行環境を確認してから読み込み直してください。')
                return
            if current:
                with self.lock:
                    if serial != self.serial:
                        return
                    memory = self.state['memory']
                    memory['latest'] = current
                    memory['peak_swap_growth_mb'] = round(max(memory['peak_swap_growth_mb'], current['swap'] - baseline['swap']), 2)
                    memory['min_free_percentage'] = min(memory['min_free_percentage'], current['free'])
            if current and (current['free'] < 5 or current['swap'] - baseline['swap'] > 2048):
                self._fail(p, serial, 'メモリ圧迫を検出したためモデルを停止しました（空き指標%d%%、スワップ増加%.0f MB）。他の大きなアプリを閉じ、短文から試してください。' % (current['free'], current['swap'] - baseline['swap']))
                return

    def _end_job(self, status):
        """Call with the lock held: an unfinished document job never stays 'planning' or 'running'."""
        job = self.state.get('job')
        if job and job['status'] in ('running', 'planning'):
            self.state['job'] = dict(job, status=status)

    def clear(self):
        """Erase text held by the app for every open tab; the model stays loaded."""
        with self.lock:
            if self.state['phase'] == 'translating':
                raise ValueError('翻訳中は消せません。終わるのを待つか、中断してください。')
            self.state.update(result=None, error=None, forward=None, reverse=None, pending=None, job=None, long_input=None)
            self.job_segments = []
            self.job_text = ''
            self.state['clear_serial'] += 1

    def translate(self, text, source, target, limit=256, kind='forward'):
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError('原文を1〜2000文字で入力してください。')
        if '<|plamo:' in text:
            raise ValueError('原文にPLaMoの予約トークンが含まれています。通常の文章だけを入力してください。')
        if kind == 'reverse' and AUTO in (source, target):
            raise ValueError('元の言語に戻すには、翻訳元の言語を指定してください。')
        validate_language_pair(source, target)
        if limit != 'auto' and (type(limit) is not int or limit not in (128, 256, 512)):
            raise ValueError('出力上限を選び直してください。')
        if kind not in ('forward', 'reverse'):
            raise ValueError('入力形式が正しくありません。')
        with self.lock:
            if self.state['phase'] != 'ready' or self.proc is None:
                raise ValueError('モデルの準備ができていません。処理中の場合は完了を待ってください。')
            forward = self.state['forward']
            if kind == 'reverse' and forward and forward['source'] == AUTO:
                raise ValueError('元の言語に戻すには、翻訳元の言語を指定してください。')
            if kind == 'reverse' and not (forward and forward['result']['text'] == text
                                          and forward['source'] == target and forward['target'] == source):
                # A back-translation is only ever attached to the translation it checks.
                raise ValueError('比べる翻訳結果が見つかりません。もう一度翻訳してください。')
            p, serial, q = self.proc, self.serial, self.messages
            if kind == 'forward':
                self.state.update(forward=None, reverse=None)
            else:
                self.state.update(reverse=None)
            self.state.update(phase='translating', message='このパソコンで翻訳しています。', result=None, error=None,
                              pending=dict(kind=kind, text=text, source=source, target=target))
            try:
                p.stdin.write(json.dumps(dict(text=text, source=source, target=target, limit=limit), ensure_ascii=False) + '\n')
                p.stdin.flush()
            except (OSError, ValueError):
                self._fail(p, serial, 'モデルと通信できません。もう一度読み込んでください。')
                return
            threading.Thread(target=self._wait_translation, args=(p, q, serial), daemon=True).start()

    def _wait_translation(self, p, q, serial):
        try:
            event = self._next(p, q, serial, 180)
            if event is None:
                return
            with self.lock:
                if serial != self.serial:
                    return
                pending, self.state['pending'] = self.state['pending'], None
                if event.get('event') == 'result':
                    result = dict(event['result'], kind=pending['kind'] if pending else None)
                    self.state.update(phase='ready', result=result, message='翻訳が終わりました。', error=None)
                    if pending and pending['kind'] == 'forward':
                        self.state['forward'] = dict(text=pending['text'], source=pending['source'], target=pending['target'], result=result)
                    elif pending and pending['kind'] == 'reverse' and self.state['forward']:
                        self.state['reverse'] = result
                elif event.get('event') == 'input_error':
                    self.state.update(phase='ready', error=event['message'], message='入力を確認してください。')
                    if event.get('code') == 'too_long' and pending and pending['kind'] == 'forward':
                        self.state['long_input'] = dict(text=pending['text'], source=pending['source'], target=pending['target'])
                else:
                    raise ValueError(event.get('message', '翻訳に失敗しました。'))
        except Exception as e:
            self._fail(p, serial, str(e))


CHUNK_CHOICES = (300, 600, 1000, 2000)
CONTEXT_CHOICES = (0, 1, 2, 4)
BUDGET_RATIO = 0.8
MAX_RESPLIT_DEPTH = 3


def validate_glossary(glossary):
    if glossary in (None, []):
        return None
    if not isinstance(glossary, list) or len(glossary) > 100:
        raise ValueError('用語集は100行までです。')
    clean = []
    for term in glossary:
        if not isinstance(term, dict) or set(term) != {'source', 'target'}:
            raise ValueError('用語集の形式が正しくありません。')
        a, b = term['source'], term['target']
        if not all(isinstance(x, str) and x.strip() and len(x) <= 200 and '<|plamo:' not in x for x in (a, b)):
            raise ValueError('用語集の形式が正しくありません。')
        clean.append(dict(source=a.strip(), target=b.strip()))
    return clean


def output_reserve(text_tokens):
    return max(64, int(text_tokens * 1.5) + 32)


def translate_document(self, text, source, target, chunk=600, context=1, glossary=None, name=''):
    if not isinstance(text, str) or not text.strip():
        raise ValueError('翻訳する文章がありません。')
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError('文章が長すぎます（20万文字まで）。文書を分けてください。')
    if '<|plamo:' in text:
        raise ValueError('原文にPLaMoの予約トークンが含まれています。通常の文章だけを入力してください。')
    validate_language_pair(source, target)
    if type(chunk) is not int or chunk not in CHUNK_CHOICES:
        raise ValueError('一度に翻訳する長さを選び直してください。')
    if type(context) is not int or context not in CONTEXT_CHOICES:
        raise ValueError('前後の文脈の設定を選び直してください。')
    glossary = validate_glossary(glossary)
    if not isinstance(name, str):
        name = ''
    name = re.sub(r'[\x00-\x1f/\\:*?"<>|]+', '_', name)[:80]
    fingerprint = hashlib.sha256(text.encode('utf-8')).hexdigest()
    segments = split_segments(text, chunk)
    with self.lock:
        if self.state['phase'] != 'ready' or self.proc is None:
            raise ValueError('モデルの準備ができていません。処理中の場合は完了を待ってください。')
        p, serial, q = self.proc, self.serial, self.messages
        cap = (self.state.get('model') or {}).get('context_token_cap') or 4096
        self.job_cancel = False
        self.job_counter = getattr(self, 'job_counter', 0) + 1
        self.job_segments = [dict(source=x['text'], after=x['after'], translation=None, warning=None, context_used=0, reserve=None) for x in segments]
        self.job_text = text  # kept in memory only, so a reloaded page can show what was translated
        self.state.update(phase='translating', message='トークン数を数えて、分け方を決めています。', result=None, error=None, long_input=None,
                          job=dict(total=len(segments), done=0, status='planning', chunk=chunk, context=context,
                                   source=source, target=target, cut_off=0, failed=0, context_reduced=0, resplit=0,
                                   planned_splits=0, seconds=0, budget=int(cap * BUDGET_RATIO), cap=cap,
                                   glossary=len(glossary or []), id=self.job_counter, name=name, text_sha256=fingerprint))
        owner = self.state['job']
    threading.Thread(target=self._run_document, args=(p, q, serial, source, target, context, glossary),
                     kwargs={'owner': owner}, daemon=True).start()
    return len(segments)


def _send(self, p, serial, request):
    try:
        p.stdin.write(json.dumps(request, ensure_ascii=False) + '\n')
        p.stdin.flush()
        return True
    except (OSError, ValueError):
        self._fail(p, serial, 'モデルと通信できません。もう一度読み込んでください。')
        return False


def _resplit(segment_text, after):
    """Split one part again at paragraph/sentence breaks; the last piece keeps `after`."""
    limit = max(100, min(2000, len(segment_text) // 2 + 1))
    pieces = split_segments(segment_text, limit)
    if len(pieces) < 2:
        return None
    pieces[-1]['after'] = after
    return pieces


def _plan_document(self, p, q, serial, source, target, glossary, owner=None):
    """Count real tokens, then atomically apply the plan to its owning job only."""
    owner = owner if owner is not None else self.state.get('job')
    depth = 0
    while True:
        with self.lock:
            if owner is None or self.state.get('job') is not owner or serial != self.serial:
                return False
            budget = owner['budget']
            todo = [s for s in self.job_segments if s['reserve'] is None]
            if not todo:
                return True
            request = dict(op='count', items=[s['source'] for s in todo], source=source, target=target, glossary=glossary)
        if not self._send(p, serial, request):
            return False
        event = self._next(p, q, serial, 300)
        # Stop/reload may have replaced the job while counting. Do not read or
        # mutate the replacement's segments, counters or status at any point.
        with self.lock:
            if self.state.get('job') is not owner or serial != self.serial or event is None or self.job_cancel:
                return False
            if event.get('event') != 'counts':
                raise ValueError(event.get('message', 'トークン数を数えられませんでした。'))
            rebuilt = []
            counts = iter(event['counts'])
            for segment in self.job_segments:
                if segment['reserve'] is not None:
                    rebuilt.append(segment)
                    continue
                prompt_tokens, text_tokens = next(counts)
                reserve = output_reserve(text_tokens)
                if prompt_tokens + reserve <= budget:
                    segment.update(reserve=reserve, tokens=prompt_tokens)
                    rebuilt.append(segment)
                    continue
                pieces = _resplit(segment['source'], segment['after']) if depth < 8 else None
                if pieces:
                    rebuilt.extend(dict(source=x['text'], after=x['after'], translation=None, warning=None, context_used=0, reserve=None) for x in pieces)
                    owner['planned_splits'] += 1
                else:
                    segment.update(reserve=reserve, tokens=prompt_tokens, warning='この部分は文の途中でも分けられないほど長く、上限を超える可能性があります。結果を確認してください。')
                    rebuilt.append(segment)
            depth += 1
            self.job_segments = rebuilt
            owner['total'] = len(rebuilt)


def _run_document(self, p, q, serial, source, target, context, glossary=None, owner=None):
    started = time.monotonic()
    with self.lock:
        owner = owner if owner is not None else self.state.get('job')
        if owner is None or self.state.get('job') is not owner:
            return
    def stop_cancelled():
        if self.state.get('job') is not owner or serial != self.serial:
            return
        owner.update(status='cancelled', seconds=round(time.monotonic() - started, 1))
        self.state.update(phase='ready', message='文書の翻訳を中断しました。ここまでの結果は残っています。')
    stop_cancelled.owner = owner
    try:
        self._run_document_inner(p, q, serial, source, target, context, glossary, started, stop_cancelled)
    finally:
        with self.lock:
            job = self.state.get('job')
            if job is owner and job['status'] in ('running', 'planning'):
                # Ended without reaching done/cancelled (stopped, failed, or superseded).
                if serial == self.serial and self.state.get('job') is owner and self.job_cancel:
                    stop_cancelled()
                else:
                    self._end_job('cancelled' if self.state['phase'] == 'idle' else 'error')
                    if serial == self.serial and self.state['phase'] == 'translating':
                        self.state.update(phase='ready', message='文書の翻訳を中断しました。ここまでの結果は残っています。')


def _run_document_inner(self, p, q, serial, source, target, context, glossary, started, stop_cancelled):
    owner = stop_cancelled.owner
    try:
        if not self._plan_document(p, q, serial, source, target, glossary, owner):
            with self.lock:
                if serial == self.serial and self.state.get('job') is owner and self.job_cancel:
                    stop_cancelled()
            return
        with self.lock:
            if serial != self.serial or self.state.get('job') is not owner:
                return
            self.state['job']['status'] = 'running'
            self.state['message'] = 'このパソコンで文書を翻訳しています。'
        index = 0
        while index < len(self.job_segments):
            segment = self.job_segments[index]
            with self.lock:
                if serial != self.serial or self.state.get('job') is not owner:
                    return
                if self.job_cancel:
                    stop_cancelled(); return
            history = [dict(source=x['source'], translation=x['translation'])
                       for x in self.job_segments[max(0, index - context):index] if x['translation']] if context else []
            request = dict(text=segment['source'], source=source, target=target, limit='auto',
                           reserve=segment.get('reserve'), history=history, glossary=glossary)
            if not self._send(p, serial, request):
                return
            event = self._next(p, q, serial, 900)
            if event is None:
                return
            with self.lock:
                if serial != self.serial or self.state.get('job') is not owner:
                    return
                job = self.state['job']
                if event.get('event') == 'result' and (self.job_cancel or event['result'].get('cancelled')):
                    stop_cancelled(); return
                if event.get('event') == 'result':
                    result = event['result']
                    cut = bool(result.get('warning'))
                    if cut and segment.get('depth', 0) < MAX_RESPLIT_DEPTH:
                        pieces = _resplit(segment['source'], segment['after'])
                        if pieces:
                            # The translation was cut off: split this part again and retry.
                            new = [dict(source=x['text'], after=x['after'], translation=None, warning=None, context_used=0,
                                        reserve=None, depth=segment.get('depth', 0) + 1) for x in pieces]
                            self.job_segments[index:index + 1] = new
                            job['resplit'] += 1
                            job['total'] = len(self.job_segments)
                            continue
                    segment.update(translation=result['text'], context_used=result.get('context_used', 0),
                                   warning=result.get('warning') or segment.get('warning'))
                    if cut:
                        job['cut_off'] += 1
                    if result.get('context_used', 0) < len(history):
                        job['context_reduced'] += 1
                elif event.get('event') == 'input_error':
                    segment.update(translation='', warning=event['message'])
                    job['failed'] += 1
                else:
                    raise ValueError(event.get('message', '翻訳に失敗しました。'))
                index += 1
                job['done'] = index
                job['seconds'] = round(time.monotonic() - started, 1)
        with self.lock:
            if serial == self.serial and self.state.get('job') is owner:
                owner.update(status='done', seconds=round(time.monotonic() - started, 1))
                self.state.update(phase='ready', message='文書の翻訳が終わりました。')
    except Exception as e:
        with self.lock:
            if self.state.get('job') is not owner or serial != self.serial:
                return
            self._fail(p, serial, str(e))
            if owner['status'] in ('running', 'planning'):
                owner['status'] = 'error'


def cancel_document(self):
    with self.lock:
        if self.state.get('job') and self.state['job']['status'] in ('running', 'planning'):
            self.job_cancel = True
            # Interrupt the part being generated now (macOS); its partial text is discarded.
            if self.proc is not None:
                try:
                    self.proc.stdin.write(json.dumps(dict(op='cancel')) + '\n')
                    self.proc.stdin.flush()
                except (OSError, ValueError):
                    pass
                if hasattr(signal, 'SIGUSR1'):
                    try:
                        self.proc.send_signal(signal.SIGUSR1)
                    except OSError:
                        pass
            self.state['message'] = '中断しています。今の部分が終わったら止まります。'


def document_result(self):
    with self.lock:
        job = dict(self.state['job']) if self.state.get('job') else None
        target = job['target'] if job else 'ja'
        hidden = ('reserve', 'tokens', 'depth')
        return dict(job=job, source_text=getattr(self, 'job_text', '') if job else '', segments=[dict({k: v for k, v in x.items() if k not in hidden}, after=joiner(x['after'], target),
                                            source_after=joiner(x['after'], job['source'] if job else 'ja'))
                                       for x in self.job_segments])


def clear_document(self):
    with self.lock:
        if self.state.get('job') and self.state['job']['status'] in ('running', 'planning'):
            raise ValueError('文書を翻訳中です。先に中断してください。')
        self.job_text = ''
        self.job_segments = []
        self.state['job'] = None


Engine.translate_document = translate_document
Engine._run_document = _run_document
Engine._run_document_inner = _run_document_inner
Engine._plan_document = _plan_document
Engine._send = _send
Engine.cancel_document = cancel_document
Engine.document_result = document_result
Engine.clear_document = clear_document
