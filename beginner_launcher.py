"""Beginner launcher: ordinary CPython, explicit Chrome, loopback-only app.

Standard library only. No installer, profile changes, URL/log file, port takeover,
or process-name termination. The one-use launch fragment is sent only to Chrome.
"""
import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import platform
import signal
import struct
import subprocess
import sys
import sysconfig
import threading

DEFAULT_PORT = 50736
SUPPORTED_MINORS = (12, 13, 14)
CHROME_DOWNLOAD = 'https://www.google.com/chrome/'
PYTHON_DOWNLOADS = {
    'win32': 'https://www.python.org/downloads/windows/',
    'darwin': 'https://www.python.org/downloads/macos/',
}
BASE = Path(__file__).resolve().parent


def runtime_info():
    return {
        'executable': os.path.abspath(sys.executable),
        'version': tuple(sys.version_info[:3]),
        'releaselevel': sys.version_info.releaselevel,
        'implementation': platform.python_implementation(),
        'platform': sys.platform,
        'machine': platform.machine().lower(),
        'bits': struct.calcsize('P') * 8,
        'free_threaded': bool(sysconfig.get_config_var('Py_GIL_DISABLED')),
        'windows_build': sys.getwindowsversion().build if sys.platform == 'win32' else None,
    }


def runtime_problem(info):
    if info['implementation'] != 'CPython':
        return '通常版のCPythonが必要です。別のPython実装は未検証です。'
    if info['free_threaded']:
        return 'free-threaded版のPythonは未検証です。通常版を使用してください。'
    if info['releaselevel'] != 'final':
        return '開発版・ベータ版のPythonは未検証です。正式版を使用してください。'
    if info['version'][0] != 3 or info['version'][1] not in SUPPORTED_MINORS:
        return 'この初心者版の起動対象はPython 3.12〜3.14です。3.15以降やApple付属の旧版は未検証のため起動しません。'
    if info['bits'] != 64:
        return '64bit版のPythonが必要です。32bit版は対象外です。'
    if info['platform'] == 'win32':
        if info['machine'] not in ('amd64', 'x86_64', 'x64'):
            return 'Windows版の対象はx64です。Windows ARM版は未検証です。'
        if info['windows_build'] is None or info['windows_build'] < 22000:
            return 'この初心者版のWindows対象はWindows 11です。'
    elif info['platform'] == 'darwin':
        if info['machine'] not in ('arm64', 'aarch64'):
            return 'Mac版の対象はApple Siliconです。Intel版・RosettaのPythonは未検証です。'
    else:
        return 'この初心者版の対象はWindows 11 x64とMac Apple Siliconです。'
    # Do not silently select an assistant-managed runtime found through PATH.
    parts = str(info['executable']).replace('\\', '/').lower().split('/')
    if any(part.startswith('codex-') and 'runtime' in part for part in parts):
        return 'アプリ用に通常インストールしたPythonを使用してください。補助ツール同梱のPythonは対象外です。'
    return None


def print_runtime(info):
    print('使用するPython: ' + info['executable'], flush=True)
    print('Python %s / %s / %s / %sbit' % (
        '.'.join(map(str, info['version'])), info['implementation'], info['machine'], info['bits']), flush=True)


def print_python_help(system):
    print('通常版のPython 3.14（64bit）を追加するか、既存の3.12・3.13を使用してください。', flush=True)
    print('Pythonを自動インストールすることはありません。', flush=True)
    print('公式: ' + PYTHON_DOWNLOADS.get(system, 'https://www.python.org/downloads/'), flush=True)
    print('BEGINNER-START-ja.mdのPython導入手順を確認してください。', flush=True)


def windows_chrome_candidates(environ=None, registry_paths=()):
    """Pure path construction also tested on non-Windows hosts."""
    import ntpath
    environ = os.environ if environ is None else environ
    candidates = list(registry_paths)
    for key in ('LOCALAPPDATA', 'ProgramW6432', 'ProgramFiles', 'ProgramFiles(x86)'):
        root = environ.get(key)
        if root:
            candidates.append(ntpath.join(root, 'Google', 'Chrome', 'Application', 'chrome.exe'))
    result = []
    seen = set()
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        candidate = os.path.expandvars(candidate.strip().strip('"'))
        key = ntpath.normcase(candidate)
        if ntpath.isabs(candidate) and ntpath.basename(candidate).lower() == 'chrome.exe' and key not in seen:
            seen.add(key)
            result.append(candidate)
    return result


def windows_registry_chrome():
    try:
        import winreg
    except ImportError:
        return []
    found = []
    key = r'Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe'
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as handle:
                    value, kind = winreg.QueryValueEx(handle, None)
                    if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                        found.append(value)
            except OSError:
                pass
    return found


def find_chrome(system=None, is_file=None, home=None):
    system = sys.platform if system is None else system
    is_file = os.path.isfile if is_file is None else is_file
    if system == 'win32':
        candidates = windows_chrome_candidates(registry_paths=windows_registry_chrome())
    elif system == 'darwin':
        home = str(Path.home()) if home is None else str(home)
        candidates = [
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            os.path.join(home, 'Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
        ]
    else:
        candidates = []
    return next((path for path in candidates if is_file(path)), None)


def chrome_command(chrome, url, system=None):
    if (sys.platform if system is None else system) == 'darwin':
        # LaunchServices opens this exact app, including when Chrome is running.
        return ['/usr/bin/open', '-a', str(Path(chrome).parents[2]), url]
    return [chrome, url]


def open_chrome(chrome, url):
    command = chrome_command(chrome, url)
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
    if sys.platform == 'darwin':
        result = subprocess.run(command, timeout=15, **kwargs)
        if result.returncode:
            raise OSError('Chrome could not be opened')
    else:
        process = subprocess.Popen(command, **kwargs)
        try:
            code = process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return  # A new Chrome process can keep running; it is never killed.
        if code:
            raise OSError('Chrome could not be opened')


@contextmanager
def gguf_cpu_mode():
    """Limit this app's GGUF children to the tested CPU path; no OS setting."""
    previous = os.environ.get('KOTOBA_LLAMA_CPU')
    os.environ['KOTOBA_LLAMA_CPU'] = '1'
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop('KOTOBA_LLAMA_CPU', None)
        else:
            os.environ['KOTOBA_LLAMA_CPU'] = previous


@contextmanager
def stop_signals(stop_requested):
    previous = {}
    def request_stop(*_):
        stop_requested.set()
    try:
        for name in ('SIGINT', 'SIGTERM', 'SIGHUP', 'SIGBREAK'):
            number = getattr(signal, name, None)
            if number is not None:
                previous[number] = signal.signal(number, request_stop)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def run_application(server, chrome):
    """Only this server and the engine it owns are stopped, including on failure."""
    requested = threading.Event()
    errors = []
    def serve():
        try:
            server.serve_forever(poll_interval=0.2)
        except Exception:
            errors.append(True)
        finally:
            requested.set()
    thread = threading.Thread(target=serve, name='kotoba-local-server', daemon=True)
    with stop_signals(requested):
        try:
            thread.start()
            # Never print, persist, or reuse this one-time authentication URL.
            url = 'http://127.0.0.1:%s/#code=%s' % (server.server_port, server.launch['code'])
            if not requested.is_set():
                open_chrome(chrome, url)
                print('Chromeで開きました。この起動ウィンドウは使用中、そのままにしてください。', flush=True)
                print('終了: アプリ画面の「アプリを終了」、またはこのウィンドウでCtrl+C。', flush=True)
                print('Chromeのタブを閉じただけではアプリは終了しません。', flush=True)
            requested.wait()
            if errors:
                print('アプリを続けられないため停止しました。起動プログラムを開き直してください。', flush=True)
                return 1
            return 0
        except KeyboardInterrupt:
            return 0
        except (OSError, subprocess.SubprocessError):
            # Exception strings can contain the secret URL; show only fixed text.
            print('Chromeを開けなかったため、アプリを停止しました。Chromeを起動できるか確認してから再試行してください。', flush=True)
            return 3
        finally:
            try:
                if thread.is_alive():
                    server.shutdown()
                    thread.join(timeout=5)
            finally:
                try:
                    server.engine.stop()
                finally:
                    server.server_close()
            print('ことばの窓を終了しました。', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='ことばの窓 初心者向け起動プログラム')
    parser.add_argument('--check-python', action='store_true', help='Pythonだけを確認して終了')
    parser.add_argument('--diagnose', action='store_true', help='PythonとChromeの場所を確認。アプリは起動しません')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error('ポート番号は0〜65535です。通常は指定不要です。')
    info = runtime_info()
    problem = runtime_problem(info)
    if args.check_python:
        if problem:
            print_runtime(info)
            print(problem, flush=True)
            print_python_help(info['platform'])
        return 2 if problem else 0
    print('ことばの窓 0.8 初心者向け試作版', flush=True)
    print_runtime(info)
    if problem:
        print(problem, flush=True)
        print_python_help(info['platform'])
        return 2
    print('過去の翻訳実証: Windows CPython 3.12.14 x64 / Mac CPython 3.13.18（M6・32GB）。', flush=True)
    print('3.12〜3.14すべての互換性・最低版は未確定です。3.14は別インストールでの試用対象です。', flush=True)
    print('今回のGGUF翻訳はCPUのみで実行します（MLXには影響しません）。', flush=True)
    if info['version'][1] == 14:
        print('今回はPython 3.14の試用です。過去の実証版と同一ではありません。', flush=True)
    chrome = find_chrome()
    if not chrome:
        print('Google Chromeが見つかりません。Chromeの通常版をインストールして、起動プログラムを開き直してください。', flush=True)
        print('公式: ' + CHROME_DOWNLOAD, flush=True)
        print('自動インストール・既定ブラウザの変更はしません。BEGINNER-START-ja.mdを確認してください。', flush=True)
        return 3
    print('使用するChrome: ' + chrome, flush=True)
    if args.diagnose:
        print('診断のみ完了しました。Chrome・翻訳アプリは起動していません。', flush=True)
        return 0
    # -I keeps user-site/PYTHONPATH out; add only this verified script directory.
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(BASE))
    try:
        from app import create_server
    except (ImportError, OSError):
        print('アプリのファイルを読み込めません。ZIPをすべて展開し、フォルダ内のファイルを一緒に置いて起動してください。', flush=True)
        print('解決しない場合はBEGINNER-START-ja.mdのトラブル対処を確認してください。', flush=True)
        return 5
    os.umask(0o077)
    with gguf_cpu_mode():
        try:
            server = create_server(args.port)
        except OSError:
            print('起動できません。ポート%sが使用中の可能性があります。' % args.port, flush=True)
            print('先に開いた「ことばの窓」のChrome画面と起動ウィンドウを確認してください。', flush=True)
            print('別のアプリやプロセスは停止しません。先の画面で「アプリを終了」してから再試行してください。', flush=True)
            return 4
        return run_application(server, chrome)


if __name__ == '__main__':
    sys.exit(main())
