"""Fail-closed HTTP transport for a directly owned, local IPv4 server.

Before sending HTTP headers or a body, bind the established TCP connection's
reverse tuple to the child PID using the OS. No automatic reconnect is allowed.
This does not defend against an attacker with authority to inject code into the
owned process or modify this application. No third-party packages are required.
"""
import ctypes
import functools
import http.client
import socket
import struct
import subprocess
import sys
import time


class OwnershipError(OSError):
    """The connected peer could not be tied to the live owned process."""


# Fixed-width DWORDs matter on non-Windows hosts used for fixture tests.
DWORD = ctypes.c_uint32
class TcpRow(ctypes.Structure):
    _fields_ = [(name, DWORD) for name in ('state', 'local_addr', 'local_port',
                                         'remote_addr', 'remote_port', 'pid')]


@functools.lru_cache(maxsize=1)
def _windows_table_api():
    # Do not resolve this security dependency through cwd or PATH.
    dll = ctypes.WinDLL('iphlpapi.dll', use_last_error=True, winmode=0x00000800)
    fn = dll.GetExtendedTcpTable
    fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(DWORD), ctypes.c_int,
                   DWORD, ctypes.c_int, DWORD]
    fn.restype = DWORD
    return fn


def _windows_owns(pid, server, client, api=None):
    """MIB_TCPTABLE_OWNER_PID: exact established server-side IPv4 tuple."""
    api = api or _windows_table_api()
    size = DWORD(0)
    code = api(None, ctypes.byref(size), False, socket.AF_INET, 5, 0)
    if code not in (0, 122):  # ERROR_INSUFFICIENT_BUFFER
        raise OwnershipError('TCP owner query failed (%d)' % code)
    for _ in range(3):
        if not 4 <= size.value <= 32 * 1024 * 1024:
            raise OwnershipError('Invalid TCP owner table size')
        allocated = size.value
        buf = ctypes.create_string_buffer(allocated)
        code = api(buf, ctypes.byref(size), False, socket.AF_INET, 5, 0)
        if code == 122:
            continue
        if code != 0:
            raise OwnershipError('TCP owner query failed (%d)' % code)
        count = struct.unpack_from('=I', buf.raw)[0]
        if 4 + count * ctypes.sizeof(TcpRow) > allocated:
            raise OwnershipError('Truncated TCP owner table')
        for i in range(count):
            row = TcpRow.from_buffer_copy(buf, 4 + i * ctypes.sizeof(TcpRow))
            if row.state != 5 or row.pid != pid:  # MIB_TCP_STATE_ESTAB
                continue
            local = (socket.inet_ntoa(struct.pack('=I', row.local_addr)),
                     socket.ntohs(row.local_port & 0xffff))
            remote = (socket.inet_ntoa(struct.pack('=I', row.remote_addr)),
                      socket.ntohs(row.remote_port & 0xffff))
            if local == server and remote == client:
                return True
        return False
    raise OwnershipError('TCP owner table kept changing')


def _lsof_matches(data, pid, server, client):
    """Parse NUL-delimited process/file records; never match another process."""
    current_pid = None
    endpoint = '%s:%d->%s:%d' % (*server, *client)
    for field in data.split(b'\0'):
        field = field.lstrip(b'\r\n')
        if not field:
            continue
        if field[:1] == b'p':
            try:
                current_pid = int(field[1:])
            except ValueError:
                current_pid = None
        elif field[:1] == b'n' and current_pid == pid:
            if field[1:] == endpoint.encode('ascii'):
                return True
    return False


def _mac_owns(pid, server, client, timeout=1.0):
    # lsof sees accepted sockets, so a newly connected socket may need a short
    # retry before the server's accept() runs. The caller bounds all retries.
    try:
        result = subprocess.run(
            ['/usr/sbin/lsof', '-nP', '-a', '-p', str(pid), '-i4TCP',
             '-sTCP:ESTABLISHED', '-F0pn'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=max(.01, timeout), check=False,
            env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C'})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OwnershipError('Cannot inspect the local TCP owner') from exc
    if result.returncode not in (0, 1):
        raise OwnershipError('Cannot inspect the local TCP owner')
    # lsof 1 with no records means accept() has not made a matching FD yet.
    if result.returncode == 1 and result.stderr.strip():
        raise OwnershipError('Cannot inspect the local TCP owner')
    return _lsof_matches(result.stdout, pid, server, client)


def socket_owned_by(sock, pid, *, timeout=1.0):
    """Only IPv4 loopback and a positive PID are eligible. Unknown OS fails closed."""
    if type(pid) is not int or pid <= 0:
        raise OwnershipError('Invalid server process ID')
    if sock.family != socket.AF_INET:
        raise OwnershipError('Expected an IPv4 loopback socket')
    client, server = sock.getsockname(), sock.getpeername()
    if client[0] != '127.0.0.1' or server[0] != '127.0.0.1':
        raise OwnershipError('Expected an IPv4 loopback socket')
    if sys.platform == 'win32':
        return _windows_owns(pid, server, client)
    if sys.platform == 'darwin':
        return _mac_owns(pid, server, client, timeout)
    raise OwnershipError('TCP owner inspection is unsupported on this OS')


class OwnedHTTPConnection(http.client.HTTPConnection):
    """One explicitly connected HTTP socket; no reconnect or HTTP proxy tunnel."""
    def __init__(self, port, owner, timeout=600, ownership_timeout=1.0):
        super().__init__('127.0.0.1', port, timeout=timeout)
        self.owner = owner
        self._owner_pid = owner.pid
        self._verified = False
        self.ownership_timeout = ownership_timeout
        self.auto_open = 0
        self._connect_started = False

    def set_tunnel(self, *args, **kwargs):
        raise OwnershipError('Proxy tunnels are not permitted')

    def _alive(self):
        if type(self._owner_pid) is not int or self._owner_pid <= 0 or self.owner.pid != self._owner_pid:
            raise OwnershipError('The owned server process identity is invalid')
        if self.owner.poll() is not None:
            raise OwnershipError('The owned server has ended')

    def send(self, data):
        # Also reject accidental cross-thread use while connect() is inspecting.
        # auto_open alone cannot protect an already-created, unverified socket.
        if not self._verified:
            raise http.client.NotConnected()
        return super().send(data)

    def close(self):
        self._verified = False
        super().close()

    def connect(self):
        if self._connect_started:
            raise OwnershipError('Reconnecting an owned socket is not permitted')
        self._connect_started = True
        self._alive()
        try:
            super().connect()
            deadline = time.monotonic() + self.ownership_timeout
            peer = self.sock.getpeername()
            local = self.sock.getsockname()
            while True:
                self._alive()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OwnershipError('The local connection is not owned by the server')
                if socket_owned_by(self.sock, self._owner_pid, timeout=remaining):
                    self._alive()
                    if self.sock.getpeername() != peer or self.sock.getsockname() != local:
                        raise OwnershipError('The local connection changed during inspection')
                    self._verified = True
                    return
                time.sleep(min(.01, max(0, remaining)))
        except BaseException:
            self.close()
            raise