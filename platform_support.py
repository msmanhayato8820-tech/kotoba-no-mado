"""OS-specific helpers (macOS and Windows). Standard library only; nothing is installed."""
import ctypes
import os
import platform
import re
import subprocess
import sys


def host():
    """('darwin'|'win32'|other, 'arm64'|'x64'|other)"""
    machine = platform.machine().lower()
    if machine in ('amd64', 'x86_64'):
        machine = 'x64'
    elif machine in ('aarch64', 'arm64'):
        machine = 'arm64'
    return sys.platform, machine


def _mac_memory():
    a = subprocess.check_output(['/usr/bin/memory_pressure', '-Q'], text=True, timeout=3)
    b = subprocess.check_output(['/usr/sbin/sysctl', 'vm.swapusage'], text=True, timeout=3)
    return dict(free=int(re.search(r'free percentage: (\d+)%', a).group(1)),
                swap=float(re.search(r'used = ([\d.]+)M', b).group(1)))


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]


def _windows_memory():
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(_MemoryStatusEx)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError('GlobalMemoryStatusEx failed')
    free = int(status.ullAvailPhys * 100 / status.ullTotalPhys)
    # Windows has no single "swap used" value; the part of the commit charge that exceeds
    # physical memory is used as the equivalent, in MB, so growth means paging to disk.
    commit = status.ullTotalPageFile - status.ullAvailPageFile
    swap = max(0, commit - status.ullTotalPhys) / (1024 * 1024)
    return dict(free=free, swap=round(swap, 1), total_bytes=status.ullTotalPhys)


def memory_snapshot():
    """{'free': percent of memory available, 'swap': MB} or None if it cannot be measured."""
    try:
        if sys.platform == 'darwin':
            return _mac_memory()
        if sys.platform == 'win32':
            return _windows_memory()
    except Exception:
        return None
    return None


_NETWORK_FS = frozenset(('smbfs', 'nfs', 'afpfs', 'webdav', 'cifs', 'ftp', 'macfuse', 'osxfuse'))


class _StatFs(ctypes.Structure):
    # macOS 64-bit struct statfs (the default layout on Apple Silicon).
    _fields_ = [('f_bsize', ctypes.c_uint32), ('f_iosize', ctypes.c_int32), ('f_blocks', ctypes.c_uint64),
                ('f_bfree', ctypes.c_uint64), ('f_bavail', ctypes.c_uint64), ('f_files', ctypes.c_uint64),
                ('f_ffree', ctypes.c_uint64), ('f_fsid', ctypes.c_int32 * 2), ('f_owner', ctypes.c_uint32),
                ('f_type', ctypes.c_uint32), ('f_flags', ctypes.c_uint32), ('f_fssubtype', ctypes.c_uint32),
                ('f_fstypename', ctypes.c_char * 16), ('f_mntonname', ctypes.c_char * 1024),
                ('f_mntfromname', ctypes.c_char * 1024), ('f_flags_ext', ctypes.c_uint32), ('f_reserved', ctypes.c_uint32 * 7)]


def _mac_filesystem(path):
    libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    info = _StatFs()
    if libc.statfs(os.fsencode(str(path)), ctypes.byref(info)) != 0:
        return None
    name = info.f_fstypename.decode('ascii', 'replace')
    if not name or not name.isprintable():
        return None  # unexpected layout: do not guess
    return name, bool(info.f_flags & 0x00001000)  # MNT_LOCAL


def network_location(path):
    """True on a network location, False when local, None when it cannot be determined."""
    try:
        if sys.platform == 'win32':
            text = str(path)
            if text.startswith(('\\\\', '//')):
                return True
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
            kernel32.GetDriveTypeW.restype = ctypes.c_uint
            kind = kernel32.GetDriveTypeW(os.path.splitdrive(text)[0] + '\\')
            return True if kind == 4 else False if kind in (2, 3, 5, 6) else None  # 4 = DRIVE_REMOTE
        if sys.platform == 'darwin':
            found = _mac_filesystem(path)
            if found is None:
                return None
            name, local = found
            return name in _NETWORK_FS or not local
    except Exception:
        return None
    return None


def pick_folder(prompt):
    """Ask the person to choose a folder with the OS dialog. Returns a path, '' if cancelled."""
    if sys.platform == 'darwin':
        script = 'POSIX path of (choose folder with prompt "' + prompt.replace('"', '') + '")'
        result = subprocess.run(['/usr/bin/osascript', '-e', script], capture_output=True, text=True, timeout=120)
        return result.stdout.strip() if result.returncode == 0 else ''
    if sys.platform == 'win32':
        # PowerShell ships with Windows; the dialog is shown by Windows itself.
        command = ('Add-Type -AssemblyName System.Windows.Forms;'
                   '$d=New-Object System.Windows.Forms.FolderBrowserDialog;'
                   '$d.Description=$env:KOTOBA_PROMPT;$d.ShowNewFolderButton=$false;'
                   'if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){[Console]::Out.Write($d.SelectedPath)}')
        env = dict(os.environ, KOTOBA_PROMPT=prompt)
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-STA', '-Command', command],
                                capture_output=True, text=True, timeout=120, env=env,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return result.stdout.strip() if result.returncode == 0 else ''
    raise ValueError('モデルの場所を直接入力してください。')
