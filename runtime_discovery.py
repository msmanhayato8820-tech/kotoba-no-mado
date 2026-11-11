"""Read-only, bounded discovery of the official 2B preparation runtime cache.

No download, directory creation, model selection, persistent preference or consent.
The preview is advisory; core rechecks the complete runtime on every load.
"""
import stat
import threading
from model_setup import _load_spec, _data_root, _aliases, DEFAULT_SETUP_MODEL_ID
import platform_support


def _check_path(path):
    """Check each ancestor before its descendants; never resolve a refused link.

    lstat does not follow the final component, but it does follow parents. Walk
    from the anchor down so a symlink or Windows reparse point is refused before
    any child metadata lookup. Stop on the first missing ancestor as well.
    """
    if str(path).startswith(('\\\\', '//')) or not path.is_absolute():
        raise ValueError('自動検出ではネットワーク上の保存先を調べません。')
    for current in (*reversed(path.parents), path):
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('保存先にリンクがあるため、安全のため準備を中止しました。')
        if current != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError('保存先がフォルダではありません。')


def _windows_drive_type(anchor):
    # Query the local Windows drive mapping before any cache path metadata.
    import ctypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    query = kernel32.GetDriveTypeW
    query.argtypes = [ctypes.c_wchar_p]
    query.restype = ctypes.c_uint
    return query(anchor)


def prepared_model_folder():
    """Read-only hint: the 2B folder saved by automatic preparation, or ''.

    Same fixed location and link/network refusals as the runtime cache. Only the
    pinned file name and size are compared here; the SHA-256 is checked when the
    person chooses to load it. Never selects, loads or consents to anything.
    """
    try:
        folder, spec = prepared_runtime_location()
        folder = folder.parent.parent / 'models' / spec['model_id'] / spec['revision']
        weight = folder / spec['model']['name']
        _check_path(weight)
        if ({p.name for p in folder.iterdir()} != {weight.name} or not stat.S_ISREG(weight.lstat().st_mode)
                or weight.lstat().st_size != spec['model']['bytes']):
            return ''
        return str(folder)
    except Exception:
        return ''


def expected_runtime_folder():
    """Where the cache would be, as text for the screen. Touches no file or folder."""
    host = '-'.join(platform_support.host())
    spec = _load_spec(DEFAULT_SETUP_MODEL_ID, host)
    return str(_data_root(host) / 'runtimes' / ('llama.cpp-' + spec['release'] + '-' + host))


def prepared_runtime_location():
    host = '-'.join(platform_support.host())
    spec = _load_spec(DEFAULT_SETUP_MODEL_ID, host)
    root = _data_root(host)
    # Reject UNC and device paths before even checking filesystem metadata.
    if str(root).startswith(('\\\\', '//')):
        raise ValueError('自動検出ではネットワーク上の保存先を調べません。')
    if host == 'win32-x64':
        try:
            kind = _windows_drive_type(root.anchor)
        except (AttributeError, OSError, TypeError, ValueError):
            raise ValueError('自動検出では保存先がローカルのドライブか確認できません。') from None
        # Only known local drive types: removable, fixed, CD-ROM and RAM disk.
        if kind == 4:  # DRIVE_REMOTE includes mapped network drive letters.
            raise ValueError('自動検出ではネットワーク上の保存先を調べません。')
        if kind not in (2, 3, 5, 6):
            raise ValueError('自動検出では保存先がローカルのドライブか確認できません。')
    folder = root / 'runtimes' / ('llama.cpp-' + spec['release'] + '-' + host)
    _check_path(folder)
    return folder, spec


def check_cache_layout(folder, spec):
    # Match ModelSetupManager._existing_runtime: no extra files, including GGUF.
    _check_path(folder)
    if not folder.exists():
        raise FileNotFoundError('2B準備の実行プログラムが見つかりません。')
    aliases = _aliases(spec)
    if not folder.is_dir() or {p.name for p in folder.iterdir()} != set(spec['files']) | set(aliases):
        raise ValueError('2B準備の実行プログラムに不足・余分なファイルがあります。')
    for name in spec['files']:
        _check_path(folder / name)
    for name, target in aliases.items():
        path = folder / name
        if not path.is_symlink() or str(path.readlink()) != target:
            raise ValueError('2B準備の実行プログラムのリンクが固定版と異なります。')


class RuntimeDiscovery:
    def __init__(self):
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.thread = None
        self.state = dict(phase='idle', path=None)

    def status(self):
        with self.lock:
            return dict(self.state)

    def start(self):
        with self.lock:
            if self.cancelled.is_set():
                return self.status()
            if self.thread and self.thread.is_alive():
                return self.status()
            self.state = dict(phase='checking', path=None)
            self.thread = threading.Thread(target=self._check, daemon=True)
            self.thread.start()
            return self.status()

    def _check(self):
        path = None
        try:
            path = expected_runtime_folder()  # text only; shown even when missing
            folder, spec = prepared_runtime_location()
            path = str(folder)
            check_cache_layout(folder, spec)
            from core import llama_runtime
            llama_runtime('', cancelled=self.cancelled.is_set)
            phase = 'ready'
        except FileNotFoundError:
            phase = 'missing'
        except (ValueError, OSError):
            phase = 'invalid'
        except Exception:
            phase = 'unavailable'
        with self.lock:
            if not self.cancelled.is_set():
                self.state = dict(phase=phase, path=path)

    def close(self):
        self.cancelled.set()
        with self.lock:
            self.state = dict(phase='cancelled', path=None)
