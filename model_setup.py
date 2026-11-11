"""Pinned, opt-in model preparation. Standard library only; importing never downloads.

Only the first-run 2B GGUF and official b11938 CPU runtime are supported. The UI
must show plan() before start(acknowledged=True, plan_id=...). No caller-supplied
URL, destination, archive name, or platform is accepted. This module never runs
any downloaded executable or changes macOS quarantine/Gatekeeper settings.
"""
from __future__ import annotations

import copy
import errno
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import stat
import sys
import tarfile
import tempfile
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener
import uuid
import zipfile

import platform_support

BASE = Path(__file__).resolve().parent
DEFAULT_SETUP_MODEL_ID = 'plamo-2b-gguf-q4km'
MODEL_REPO = 'pfnet/plamo-3-translate-2b-GGUF'
MODEL_REVISION = '4f672106b847200bfdc0f6af6c321cd94eba1aed'
RELEASE = 'b11938'
CHUNK_SIZE = 1024 * 1024
SPACE_MARGIN = 128 * 1024 * 1024
NETWORK_TIMEOUT = 15
ACTIVE_PHASES = frozenset(('checking', 'downloading', 'verifying', 'extracting', 'cancelling'))
_PHASE_EN = {
    'idle': 'Ready to prepare the translation AI.',
    'checking': 'Checking saved files and available storage.',
    'downloading': 'Downloading from the official distribution service.',
    'verifying': 'Verifying the complete file with SHA-256.',
    'extracting': 'Extracting and verifying every runtime file.',
    'ready': 'The translation AI is ready.',
    'cancelling': 'Stopping preparation. Partial downloads will be kept for resume.',
    'cancelled': 'Preparation stopped. You can resume the partial download.',
}
_ERROR_EN = {
    'unsupported_host': 'Automatic setup supports Windows 11 x64 and Apple Silicon Macs.',
    'unsupported_model': 'Only PLaMo 3 Translate 2B GGUF Q4_K_M supports automatic setup.',
    'invalid_manifest': 'The pinned verification manifest is invalid. Check the app distribution.',
    'storage_path': 'The dedicated storage location is unavailable. Check the folder and permissions.',
    'unsafe_path': 'A storage path contains a link or special file. Preparation stopped for safety.',
    'unsafe_redirect': 'A redirect outside the official distribution services was blocked.',
    'unsafe_url': 'Downloads are restricted to the official HTTPS distribution services.',
    'setup_busy': 'Another app instance is preparing files. Wait for it to finish and try again.',
    'consent_required': 'Review the size, sources, licenses and storage location, then consent to download.',
    'plan_changed': 'Display and review the current plan before consenting to download.',
    'closed': 'Preparation cannot start while the app is closing.',
    'existing_model_invalid': 'The saved model differs from the pinned version. It was not overwritten.',
    'existing_runtime_invalid': 'The saved runtime differs from the pinned version. It was not overwritten.',
    'existing_download_invalid': 'A saved download is damaged. It was not overwritten.',
    'insufficient_space': 'Not enough free storage. Free up space at the storage location and retry.',
    'not_found': 'The pinned download was not found. Check for an updated app distribution.',
    'http_error': 'The distribution service could not complete the download. Please try again.',
    'network_error': 'The connection was interrupted. Check your connection and resume.',
    'storage_error': 'Could not save the file. Check storage permissions and free space.',
    'preparation_failed': 'Preparation stopped for safety. Check the app distribution and retry.',
    'destination_exists': 'The destination already exists. It was not overwritten.',
    'invalid_range': 'The server returned invalid resume information. The download was stopped.',
    'invalid_response': 'The server response cannot be read safely.',
    'size_mismatch': 'The download size differs from the pinned version.',
    'download_incomplete': 'The download was interrupted. Choose Resume to continue.',
    'hash_mismatch': 'File verification failed. Retrying will download this file again from the beginning.',
    'archive_hash_mismatch': 'The runtime archive failed verification.',
    'runtime_incomplete': 'The runtime archive does not contain all required files.',
    'unsafe_archive': 'The archive contains an unlisted file, unsafe path, link or invalid size.',
    'archive_size_mismatch': 'An extracted file exceeds its pinned size.',
    'runtime_hash_mismatch': 'An extracted runtime file failed verification.',
}


class SetupError(ValueError):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class _Cancelled(Exception):
    pass


def _host():
    host = '-'.join(platform_support.host())
    if host not in ('win32-x64', 'darwin-arm64'):
        raise SetupError('unsupported_host', '自動準備は Windows 11 x64 と Apple Silicon Mac に対応しています。')
    if host == 'win32-x64' and sys.getwindowsversion().build < 22000:
        raise SetupError('unsupported_host', '自動準備には Windows 11 以降が必要です。')
    return host


def _data_root(host):
    if host == 'win32-x64':
        local = os.environ.get('LOCALAPPDATA', '')
        if not local or not Path(local).is_absolute():
            raise SetupError('storage_path', 'Windows の保存先を確認できません。LOCALAPPDATA を確認してください。')
        return Path(local) / 'KotobaNoMado'
    return Path.home() / 'Library' / 'Application Support' / 'KotobaNoMado'


def _simple_name(name):
    return (isinstance(name, str) and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name))
            and name not in ('.', '..'))


def _valid_item(item):
    return (isinstance(item, dict) and type(item.get('bytes')) is int and item['bytes'] > 0
            and bool(re.fullmatch('[0-9a-f]{64}', str(item.get('sha256', '')))))


def _load_spec(model_id, host):
    if not isinstance(model_id, str) or model_id != DEFAULT_SETUP_MODEL_ID:
        raise SetupError('unsupported_model', '自動準備できる翻訳 AI は PLaMo 3 Translate 2B GGUF Q4_K_M のみです。')
    try:
        catalog = json.loads((BASE / 'model-catalog.json').read_text(encoding='utf-8'))[model_id]
        model = json.loads((BASE / 'manifests/plamo-2b-gguf-q4km.json').read_text(encoding='utf-8'))
        runtime = json.loads((BASE / 'manifests/llama-cpp.json').read_text(encoding='utf-8'))
        binary = runtime['binaries'][host]
        if (model['repo'] != MODEL_REPO or catalog['repo'] != MODEL_REPO
                or model['revision'] != MODEL_REVISION or catalog['revision'] != MODEL_REVISION
                or runtime['release'] != RELEASE or len(model['files']) != 1):
            raise ValueError('Wrong pinned version')
        weight = model['files'][0]
        if weight != {'name': 'plamo-3-translate-2b-Q4_K_M.gguf', 'bytes': 1807203040,
                          'sha256': '1ffd9073afcc70e35ccc942428aa2b73d7aa2b9ea779bf504b775369e0276c13'}:
            raise ValueError('Wrong model pin')
        expected = {
            'win32-x64': ('llama-b11938-bin-win-cpu-x64.zip', 24260998,
                          '9e2fc62604446d86809ca09094d3c7a7329b723bda636e4bbd23c3ce62cb77dc'),
            'darwin-arm64': ('llama-b11938-bin-macos-arm64.tar.gz', 16341137,
                             'b1ed33aacd0927eaa69644e6b15e3206115f7f80dfc7501de86df89dab853e9f'),
        }[host]
        if ((binary['archive'], binary['archive_bytes'], binary['archive_sha256']) != expected
                or not isinstance(binary['files'], dict) or not binary['files']
                or not all(_simple_name(n) and _valid_item(v) for n, v in binary['files'].items())
                or binary['exe'] != ('llama-server.exe' if host == 'win32-x64' else 'llama-server')
                or binary['exe'] not in binary['files']
                or len({n.casefold() for n in binary['files']}) != len(binary['files'])):
            raise ValueError('Wrong runtime pin')
    except (OSError, ValueError, KeyError, TypeError, AssertionError):
        raise SetupError('invalid_manifest', '固定版の検査情報が不正です。配布フォルダを確認してください。') from None
    model_license = f'https://huggingface.co/{MODEL_REPO}/blob/{MODEL_REVISION}/LICENSE/ja'
    runtime_license = f'https://github.com/ggml-org/llama.cpp/blob/{RELEASE}/LICENSE'
    return dict(model_id=model_id, host=host, revision=MODEL_REVISION, release=RELEASE,
                model=dict(weight, kind='model', url=f'https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{weight["name"]}',
                           license_url=model_license),
                archive=dict(name=binary['archive'], bytes=binary['archive_bytes'], sha256=binary['archive_sha256'],
                             kind='runtime', url=f'https://github.com/ggml-org/llama.cpp/releases/download/{RELEASE}/{binary["archive"]}',
                             license_url=runtime_license),
                files=binary['files'], exe=binary['exe'])


def _aliases(spec):
    """The official Mac tar has dylib aliases. Only these exact relative chains are allowed.

    ZIP links are always rejected. No arbitrary extra license/docs are accepted:
    every regular file, including LICENSE, must be in the pinned manifest.
    """
    result = {}
    if spec['host'] == 'darwin-arm64':
        for name in spec['files']:
            match = re.fullmatch(r'(lib[A-Za-z0-9_-]+)\.(\d+)\.\d+\.\d+\.dylib', name)
            if match:
                stem, major = match.groups()
                version_alias = f'{stem}.{major}.dylib'
                result[version_alias] = name
                result[f'{stem}.dylib'] = version_alias
    return result


def _check_path(path):
    """Refuse symlink/reparse-point storage ancestors, including Windows junctions."""
    for current in (path, *path.parents):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise SetupError('unsafe_path', '保存先にリンクがあるため、安全のため準備を中止しました。')


def _mkdir(path):
    _check_path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    _check_path(path)
    if not path.is_dir():
        raise SetupError('storage_path', '保存先がフォルダではありません。')


def _open_file(path, flags, mode=0o600):
    _check_path(path)
    # Check before truncating: an unexpected hard link must never modify a
    # pre-existing model or a file outside our dedicated storage directory.
    fd = os.open(path, (flags & ~os.O_TRUNC) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0), mode)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or (flags & (os.O_WRONLY | os.O_RDWR) and info.st_nlink != 1):
        os.close(fd)
        raise SetupError('unsafe_path', '保存先に通常のファイル以外や予期しないリンクが見つかりました。')
    if flags & os.O_TRUNC:
        os.ftruncate(fd, 0)
    return fd


def _free_bytes(root):
    candidate = root
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    _check_path(candidate)
    return shutil.disk_usage(candidate).free


def _allowed_url(url):
    try:
        p = urlsplit(url)
        host = (p.hostname or '').lower()
        return (p.scheme == 'https' and p.port in (None, 443) and not p.username and not p.password
                and (host in ('github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com',
                              'huggingface.co', 'hf.co')
                     or host.endswith('.huggingface.co') or host.endswith('.hf.co')))
    except ValueError:
        return False


class _Redirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed_url(newurl):
            raise SetupError('unsafe_redirect', '公式配信先以外への転送を拒否しました。')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_download(url, offset):
    if not _allowed_url(url):
        raise SetupError('unsafe_url', '公式配信先以外からは取得できません。')
    headers = {'User-Agent': 'KotobaNoMado/0.8', 'Accept-Encoding': 'identity'}
    if offset:
        headers['Range'] = f'bytes={offset}-'
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()), _Redirects())
    return opener.open(Request(url, headers=headers), timeout=NETWORK_TIMEOUT)


class _StorageLock:
    """OS advisory lock: releases automatically on crash; never executes the runtime."""
    def __init__(self, root):
        self.fd = _open_file(root / '.setup.lock', os.O_RDWR | os.O_CREAT)
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(self.fd).st_size == 0:
                    os.write(self.fd, b'0')
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise SetupError('setup_busy', '別の画面で準備中です。完了してから再試行してください。', True) from None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class ModelSetupManager:
    """One background job; plan/status are JSON-safe and have no network side effects."""
    def __init__(self):
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread = None
        self._closed = False
        self._state = dict(phase='idle', active=False, model_id=DEFAULT_SETUP_MODEL_ID,
                           progress_bytes=0, total_bytes=0, current_file=None,
                           message='翻訳 AI の準備を始められます。', error=None, error_code=None,
                           retryable=False, model_path=None, model_file=None, runtime_path=None)

    def _configuration(self, model_id):
        host = _host()
        spec = _load_spec(model_id, host)
        root = _data_root(host).absolute()
        _check_path(root)
        return spec, root

    @staticmethod
    def _paths(spec, root):
        model = root / 'models' / spec['model_id'] / spec['revision']
        runtime = root / 'runtimes' / f'llama.cpp-{spec["release"]}-{spec["host"]}'
        downloads = root / 'downloads'
        return model, runtime, downloads

    def plan(self, model_id=DEFAULT_SETUP_MODEL_ID):
        spec, root = self._configuration(model_id)
        model, runtime, downloads = self._paths(spec, root)
        files = []
        for item, destination in ((spec['model'], model / spec['model']['name']),
                                  (spec['archive'], downloads / spec['archive']['sha256'] / spec['archive']['name'])):
            files.append(dict(item, download_host=urlsplit(item['url']).hostname, save_to=str(destination)))
        download = sum(item['bytes'] for item in files)
        # Full download + archive expansion + a small safety margin. Actual reuse is
        # hash-checked in the worker, never guessed from an existing path in the UI.
        required = download + sum(item['bytes'] for item in spec['files'].values()) + SPACE_MARGIN
        token_data = dict(spec=spec, root=str(root))
        token = hashlib.sha256(json.dumps(token_data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return dict(plan_id=token, model_id=model_id, host=spec['host'], files=files,
                    download_bytes=download, required_free_bytes=required, free_bytes=_free_bytes(root),
                    save_dir=str(root), model_path=str(model), runtime_path=str(runtime),
                    runtime_file_count=len(spec['files']),
                    commercial_registration_url='https://forms.gle/mTL8tBLrMYXKNZD56',
                    model_license_url={
                        'ja': f'https://huggingface.co/{MODEL_REPO}/blob/{MODEL_REVISION}/LICENSE/ja',
                        'en': f'https://huggingface.co/{MODEL_REPO}/blob/{MODEL_REVISION}/LICENSE/en'},
                    warning_codes=['downloads_model_and_executables', 'network_and_storage_required',
                                   'hardware_not_guaranteed', 'mac_protections_unchanged'],
                    warnings_i18n={
                        'ja': ['モデルと実行プログラムを取得します。文章・文書は送信しません。',
                               '取得には通信量と空き容量が必要です。準備後の翻訳はローカルで行います。',
                               'すべてのパソコンでの動作、最低 RAM、GPU は未検証です。',
                               'Mac の保護設定は変更しません。OS の実行制限は自動解除しません。'],
                        'en': ['Downloads model weights and executables. Your text and documents are not sent.',
                               'Downloading uses network data and storage. Translation runs locally afterward.',
                               'Compatibility with every computer, minimum RAM and GPU support are unverified.',
                               'Mac security settings are not changed. OS execution restrictions are not bypassed.']},
                    download_hosts=['huggingface.co', '*.huggingface.co', 'hf.co', '*.hf.co',
                                    'github.com', 'release-assets.githubusercontent.com',
                                    'objects.githubusercontent.com'],
                    warnings=['モデルと実行プログラムを取得します。翻訳する文章・文書は送信しません。',
                              'ダウンロードには通信量と空き容量が必要です。準備後の翻訳はローカルで行います。',
                              'すべてのパソコンでの動作、最低 RAM、GPU は未検証です。',
                              'Mac の保護設定は変更しません。OS が実行を止めた場合は自動解除しません。'])

    def status(self):
        with self._lock:
            result = copy.deepcopy(self._state)
            result['active'] = self._thread is not None and self._thread.is_alive()
            code = result.get('error_code')
            english = _ERROR_EN.get(code, _PHASE_EN.get(result['phase'], 'Preparing the translation AI.'))
            result['message_i18n'] = {'ja': result['message'], 'en': english}
            result['error_i18n'] = {'ja': result['error'], 'en': english} if result['error'] else None
            return result

    def _update(self, **values):
        with self._lock:
            self._state.update(values)

    def start(self, model_id=DEFAULT_SETUP_MODEL_ID, *, acknowledged=False, plan_id=None):
        if acknowledged is not True:
            raise SetupError('consent_required', '容量・配信先・ライセンス・保存先を確認し、取得に同意してください。')
        plan = self.plan(model_id)
        if not isinstance(plan_id, str) or plan_id != plan['plan_id']:
            raise SetupError('plan_changed', '準備内容をもう一度表示してから、取得に同意してください。')
        spec, root = self._configuration(model_id)
        with self._lock:
            if self._closed:
                raise SetupError('closed', 'アプリの終了中は準備を開始できません。')
            if self._thread is not None and self._thread.is_alive():
                return self.status()
            self._cancel.clear()
            self._state.update(phase='checking', active=True, model_id=model_id, progress_bytes=0,
                               total_bytes=plan['download_bytes'], current_file=None, error=None,
                               error_code=None, retryable=False, message='保存済みファイルと空き容量を確認しています。',
                               model_path=None, model_file=None, runtime_path=None)
            self._thread = threading.Thread(target=self._run, args=(spec, root), name='model-setup', daemon=True)
            self._thread.start()
            return self.status()

    def cancel(self):
        with self._lock:
            if (self._thread is not None and self._thread.is_alive()
                    and self._state['phase'] in ACTIVE_PHASES):
                self._cancel.set()
                self._state.update(phase='cancelling', message='準備を中止しています。途中の取得内容は再開用に残します。')
            return self.status()

    def shutdown(self, timeout=20):
        with self._lock:
            self._closed = True
            self.cancel()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        return thread is None or not thread.is_alive()

    def close(self):
        return self.shutdown()

    def _check_cancel(self):
        if self._cancel.is_set():
            raise _Cancelled()

    def _verify(self, path, item):
        self._check_cancel()
        _check_path(path)
        if not path.is_file() or path.stat().st_size != item['bytes']:
            return False
        digest = hashlib.sha256()
        with os.fdopen(_open_file(path, os.O_RDONLY), 'rb') as stream:
            while True:
                self._check_cancel()
                data = stream.read(CHUNK_SIZE)
                if not data:
                    break
                digest.update(data)
        return digest.hexdigest() == item['sha256']

    def _existing_model(self, folder, spec):
        _check_path(folder)
        if not folder.exists():
            return False
        if (not folder.is_dir() or {p.name for p in folder.iterdir()} != {spec['model']['name']}
                or not self._verify(folder / spec['model']['name'], spec['model'])):
            raise SetupError('existing_model_invalid', '保存済みのモデルが固定版と異なります。上書きせず中止しました。')
        return True

    def _existing_runtime(self, folder, spec):
        _check_path(folder)
        if not folder.exists():
            return False
        aliases = _aliases(spec)
        if not folder.is_dir() or {p.name for p in folder.iterdir()} != set(spec['files']) | set(aliases):
            raise SetupError('existing_runtime_invalid', '保存済みの実行プログラムが固定版と異なります。上書きせず中止しました。')
        for name, item in spec['files'].items():
            if not self._verify(folder / name, item):
                raise SetupError('existing_runtime_invalid', '実行プログラムの検査に失敗しました。上書きせず中止しました。')
        for name, target in aliases.items():
            path = folder / name
            if not path.is_symlink() or os.readlink(path) != target:
                raise SetupError('existing_runtime_invalid', '実行プログラムのリンクが固定版と異なります。')
        return True

    def _run(self, spec, root):
        storage_lock = None
        try:
            _mkdir(root)
            storage_lock = _StorageLock(root)
            model_dir, runtime_dir, downloads = self._paths(spec, root)
            model_ready = self._existing_model(model_dir, spec)
            runtime_ready = self._existing_runtime(runtime_dir, spec)
            missing = (0 if model_ready else self._remaining_bytes(spec['model'], downloads))
            if not runtime_ready:
                missing += self._remaining_bytes(spec['archive'], downloads) + sum(x['bytes'] for x in spec['files'].values())
            if missing and _free_bytes(root) < missing + SPACE_MARGIN:
                raise SetupError('insufficient_space', '空き容量が足りません。保存先の空きを増やしてから再試行してください。', True)
            _mkdir(downloads)
            completed = spec['model']['bytes'] if model_ready else 0
            self._update(progress_bytes=completed)
            if not model_ready:
                blob = self._download(spec['model'], downloads, completed)
                self._check_cancel()
                _mkdir(model_dir.parent)
                stage = Path(tempfile.mkdtemp(prefix='.model-', dir=model_dir.parent))
                try:
                    # Hard link avoids a second 1.8 GB copy; exclusive destination.
                    os.link(blob, stage / spec['model']['name'])
                    self._publish(stage, model_dir)
                finally:
                    if stage.exists():
                        shutil.rmtree(stage)
                completed = spec['model']['bytes']
            if not runtime_ready:
                archive = self._download(spec['archive'], downloads, completed)
                self._extract(archive, runtime_dir, spec)
            self._check_cancel()
            self._update(phase='ready', progress_bytes=spec['model']['bytes'] + spec['archive']['bytes'],
                         current_file=None, message='翻訳 AI の準備ができました。',
                         model_path=str(model_dir), model_file=str(model_dir / spec['model']['name']),
                         runtime_path=str(runtime_dir), retryable=False)
        except _Cancelled:
            self._update(phase='cancelled', message='準備を中止しました。「再開」で途中から続けられます。',
                         error=None, error_code=None, retryable=True)
        except SetupError as exc:
            self._update(phase='error', message=str(exc), error=str(exc), error_code=exc.code, retryable=exc.retryable)
        except HTTPError as exc:
            if exc.fp is not None:
                exc.close()
            code = 'not_found' if exc.code == 404 else 'http_error'
            message = ('固定版の配信ファイルが見つかりません。新しい配布版を確認してください。' if exc.code == 404
                       else f'配信先が取得を完了できませんでした（HTTP {exc.code}）。再試行してください。')
            self._update(phase='error', message=message, error=message, error_code=code, retryable=exc.code != 404)
        except (URLError, TimeoutError, ConnectionError, ssl.SSLError, http.client.HTTPException):
            self._network_error()
        except OSError as exc:
            if self._cancel.is_set():
                self._update(phase='cancelled', message='準備を中止しました。途中から再開できます。', retryable=True)
            elif exc.errno == errno.ENOSPC:
                self._update(phase='error', message='空き容量が不足しました。空きを増やしてから再試行してください。',
                             error='空き容量が不足しました。', error_code='insufficient_space', retryable=True)
            else:
                self._update(phase='error', message='ファイルを保存できません。保存先の権限と空き容量を確認してください。',
                             error='ファイルを保存できません。', error_code='storage_error', retryable=True)
        except Exception:
            self._update(phase='error', message='安全のため準備を中止しました。配布ファイルを確認してください。',
                         error='準備処理に失敗しました。', error_code='preparation_failed', retryable=True)
        finally:
            if storage_lock is not None:
                storage_lock.close()
            self._update(active=False)

    def _network_error(self):
        if self._cancel.is_set():
            self._update(phase='cancelled', message='準備を中止しました。途中から再開できます。', retryable=True)
        else:
            message = '通信が中断しました。接続を確認して「再開」を押してください。'
            self._update(phase='error', message=message, error=message, error_code='network_error', retryable=True)

    def _publish(self, stage, destination):
        self._check_cancel()
        _check_path(destination)
        if destination.exists():
            raise SetupError('destination_exists', '保存先がすでに存在します。上書きせず中止しました。')
        # A cross-process advisory lock serializes all our publishers. Stage and
        # destination share a filesystem, so no partly verified folder is exposed.
        os.rename(stage, destination)

    @staticmethod
    def _remaining_bytes(item, downloads):
        folder = downloads / item['sha256']
        final = folder / item['name']
        partial = folder / (item['name'] + '.part')
        _check_path(final)
        _check_path(partial)
        if final.is_file():
            return 0  # _download will hash it before reuse.
        size = partial.stat().st_size if partial.is_file() else 0
        return item['bytes'] - size if size <= item['bytes'] else item['bytes']

    def _download(self, item, downloads, completed):
        # Including the full content hash prevents resuming a different revision.
        folder = downloads / item['sha256']
        _mkdir(folder)
        final = folder / item['name']
        partial = folder / (item['name'] + '.part')
        self._update(current_file=item['name'], phase='verifying', message='保存済みファイルを検査しています。')
        if final.exists():
            if not self._verify(final, item):
                raise SetupError('existing_download_invalid', '保存済みの取得ファイルが破損しています。上書きせず中止しました。')
            self._update(progress_bytes=completed + item['bytes'])
            return final
        _check_path(partial)
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > item['bytes']:
            self._quarantine(partial)
            offset = 0
        if offset < item['bytes']:
            self._check_cancel()
            self._update(phase='downloading', progress_bytes=completed + offset, message='公式配信先から取得しています。')
            with _open_download(item['url'], offset) as response:
                if not _allowed_url(response.geturl()):
                    raise SetupError('unsafe_redirect', '公式配信先以外への転送を拒否しました。')
                code = response.status
                if code == 200:
                    # A server may ignore Range. Start this same-revision .part
                    # again, never append a full response onto the old prefix.
                    offset = 0
                elif code == 206:
                    content_range = response.headers.get('Content-Range', '')
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', content_range)
                    if not match or tuple(map(int, match.groups())) != (offset, item['bytes'] - 1, item['bytes']):
                        raise SetupError('invalid_range', '配信先の再開情報が不正です。取得を中止しました。', True)
                else:
                    raise SetupError('http_error', '配信先から正しい取得応答がありません。', True)
                if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                    raise SetupError('invalid_response', '配信先のファイル形式が不正です。')
                length = response.headers.get('Content-Length')
                if length is not None and (not length.isdigit() or int(length) != item['bytes'] - offset):
                    raise SetupError('size_mismatch', '配信先のファイル容量が固定版と異なります。', True)
                # HTTPSResponse.read(n) may wait to fill n while a slow server
                # keeps the socket timeout alive. read1 performs at most one
                # underlying receive, so every arrival reaches the cancel check.
                # Unknown response types must fail closed, never fall back to read.
                read_chunk = getattr(response, 'read1', None)
                if not callable(read_chunk):
                    raise SetupError('invalid_response', '配信先の応答から安全に取得できません。')
                flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if offset else os.O_TRUNC)
                with os.fdopen(_open_file(partial, flags), 'wb') as stream:
                    received = offset
                    while True:
                        self._check_cancel()
                        chunk = read_chunk(min(CHUNK_SIZE, item['bytes'] - received + 1))
                        self._check_cancel()
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > item['bytes']:
                            raise SetupError('size_mismatch', '取得したファイルが予定容量を超えています。', True)
                        stream.write(chunk)
                        self._update(progress_bytes=completed + received)
                    stream.flush()
                    os.fsync(stream.fileno())
                if received != item['bytes']:
                    raise SetupError('download_incomplete', '通信が中断しました。「再開」で途中から続けられます。', True)
        self._update(phase='verifying', message='SHA-256 で取得ファイルを検査しています。')
        if not self._verify(partial, item):
            self._quarantine(partial)
            raise SetupError('hash_mismatch', 'ファイルの検査に失敗しました。再試行すると最初から取得します。', True)
        self._check_cancel()
        # link() is atomic and refuses an existing destination on both platforms.
        os.link(partial, final)
        partial.unlink()
        self._update(progress_bytes=completed + item['bytes'])
        return final

    @staticmethod
    def _quarantine(partial):
        _check_path(partial)
        os.rename(partial, partial.with_name(partial.name + '.rejected-' + uuid.uuid4().hex))

    def _extract(self, archive, destination, spec):
        self._update(phase='extracting', message='実行プログラムを展開して全ファイルを検査しています。')
        # Do not trust even a previously verified archive after an intervening step.
        if not self._verify(archive, spec['archive']):
            raise SetupError('archive_hash_mismatch', '実行プログラムの圧縮ファイルの検査に失敗しました。')
        _mkdir(destination.parent)
        stage = Path(tempfile.mkdtemp(prefix='.runtime-', dir=destination.parent))
        try:
            if spec['host'] == 'win32-x64':
                self._extract_zip(archive, stage, spec)
            else:
                self._extract_tar(archive, stage, spec)
            if not self._existing_runtime(stage, spec):
                raise SetupError('runtime_incomplete', '実行プログラムのファイルが不足しています。')
            self._publish(stage, destination)
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    @staticmethod
    def _archive_name(raw, spec):
        # Only root files or the official single release directory are permitted.
        # Backslashes, drive letters, dot components and absolute paths are rejected.
        if '\\' in raw or ':' in raw or '\x00' in raw or raw.startswith('/'):
            raise SetupError('unsafe_archive', '圧縮ファイルに安全でない保存先があります。')
        parts = raw.rstrip('/').split('/')
        if any(p in ('', '.', '..') for p in parts):
            raise SetupError('unsafe_archive', '圧縮ファイルに安全でない保存先があります。')
        if len(parts) == 2 and parts[0] == 'llama-' + spec['release']:
            parts = parts[1:]
        if len(parts) != 1 or not _simple_name(parts[0]):
            raise SetupError('unsafe_archive', '圧縮ファイルのフォルダ構成が固定版と異なります。')
        return parts[0]

    def _copy_member(self, source, target, item, executable=False):
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(_open_file(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL), 'wb') as out:
            while True:
                self._check_cancel()
                data = source.read(min(CHUNK_SIZE, item['bytes'] - size + 1))
                if not data:
                    break
                size += len(data)
                if size > item['bytes']:
                    raise SetupError('archive_size_mismatch', '展開ファイルが固定版の容量を超えています。')
                digest.update(data)
                out.write(data)
            out.flush()
            os.fsync(out.fileno())
        if size != item['bytes'] or digest.hexdigest() != item['sha256']:
            raise SetupError('runtime_hash_mismatch', '展開した実行プログラムの検査に失敗しました。')
        if executable:
            # Only our newly created Mac executable, never an existing file.
            target.chmod(0o700)

    def _extract_zip(self, archive, stage, spec):
        seen = set()
        with zipfile.ZipFile(archive) as zf:
            members = zf.infolist()
            if len(members) > len(spec['files']) + 1:
                raise SetupError('unsafe_archive', '圧縮ファイルに未登録の項目があります。')
            for member in members:
                self._check_cancel()
                name = self._archive_name(member.filename, spec)
                if member.is_dir() and member.filename == 'llama-' + spec['release'] + '/':
                    continue
                kind = stat.S_IFMT(member.external_attr >> 16)
                if (member.is_dir() or kind not in (0, stat.S_IFREG) or member.flag_bits & 1
                        or name not in spec['files'] or name.casefold() in seen):
                    raise SetupError('unsafe_archive', '圧縮ファイルに未登録の項目やリンクがあります。')
                item = spec['files'][name]
                if member.file_size != item['bytes'] or member.compress_size <= 0 or member.file_size > member.compress_size * 1000:
                    raise SetupError('unsafe_archive', '圧縮ファイルの展開容量が不正です。')
                seen.add(name.casefold())
                with zf.open(member) as source:
                    self._copy_member(source, stage / name, item)
        if seen != {name.casefold() for name in spec['files']}:
            raise SetupError('runtime_incomplete', '実行プログラムのファイルが不足しています。')

    def _extract_tar(self, archive, stage, spec):
        aliases = _aliases(spec)
        seen = set()
        # Streamed TAR avoids loading arbitrary archive metadata into memory.
        with tarfile.open(archive, 'r|gz') as tf:
            count = 0
            for member in tf:
                self._check_cancel()
                count += 1
                if count > len(spec['files']) + len(aliases) + 1:
                    raise SetupError('unsafe_archive', '圧縮ファイルに未登録の項目があります。')
                name = self._archive_name(member.name, spec)
                if member.isdir() and member.name.rstrip('/') == 'llama-' + spec['release']:
                    continue
                if name in seen:
                    raise SetupError('unsafe_archive', '圧縮ファイルに重複項目があります。')
                seen.add(name)
                if member.issym():
                    if name not in aliases or member.linkname != aliases[name] or member.size:
                        raise SetupError('unsafe_archive', '圧縮ファイルに未登録のリンクがあります。')
                    continue  # Create only after all real files have been verified.
                if not member.isfile() or member.issparse() or name not in spec['files'] or member.size != spec['files'][name]['bytes']:
                    raise SetupError('unsafe_archive', '圧縮ファイルに未登録の項目や不正な容量があります。')
                with tf.extractfile(member) as source:
                    self._copy_member(source, stage / name, spec['files'][name], executable=(name == spec['exe']))
        if seen != set(spec['files']) | set(aliases):
            raise SetupError('runtime_incomplete', '実行プログラムのファイルが不足しています。')
        for name, target in aliases.items():
            self._check_cancel()
            os.symlink(target, stage / name)
