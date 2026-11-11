"""Windows 10+ process ownership, using atomic job assignment at creation.

A non-inheritable kill-on-close job owns the server and all descendants. Supplying
PROC_THREAD_ATTRIBUTE_JOB_LIST to CreateProcessW avoids the orphan window between
Popen and AssignProcessToJobObject (even a suspended child can otherwise survive a
killed worker). Unsupported APIs or job nesting restrictions fail closed; there is
no unprotected Popen fallback. All APIs use pointer-width-safe ctypes declarations.
"""
import ctypes
import math
import subprocess

BOOL = ctypes.c_int32
DWORD = ctypes.c_uint32
WORD = ctypes.c_uint16
HANDLE = ctypes.c_void_p
SIZE_T = ctypes.c_size_t
LPVOID = ctypes.c_void_p
LPWSTR = ctypes.c_wchar_p

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
STARTF_USESTDHANDLES = 0x00000100
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
INFINITE = 0xFFFFFFFF
ERROR_INSUFFICIENT_BUFFER = 122
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class SecurityAttributes(ctypes.Structure):
    _fields_ = [('nLength', DWORD), ('lpSecurityDescriptor', LPVOID), ('bInheritHandle', BOOL)]


class BasicLimitInformation(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                ('LimitFlags', DWORD), ('MinimumWorkingSetSize', SIZE_T), ('MaximumWorkingSetSize', SIZE_T),
                ('ActiveProcessLimit', DWORD), ('Affinity', SIZE_T), ('PriorityClass', DWORD), ('SchedulingClass', DWORD)]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                                                  'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', BasicLimitInformation), ('IoInfo', IoCounters),
                ('ProcessMemoryLimit', SIZE_T), ('JobMemoryLimit', SIZE_T),
                ('PeakProcessMemoryUsed', SIZE_T), ('PeakJobMemoryUsed', SIZE_T)]


class StartupInfo(ctypes.Structure):
    _fields_ = [('cb', DWORD), ('lpReserved', LPWSTR), ('lpDesktop', LPWSTR), ('lpTitle', LPWSTR),
                ('dwX', DWORD), ('dwY', DWORD), ('dwXSize', DWORD), ('dwYSize', DWORD),
                ('dwXCountChars', DWORD), ('dwYCountChars', DWORD), ('dwFillAttribute', DWORD),
                ('dwFlags', DWORD), ('wShowWindow', WORD), ('cbReserved2', WORD), ('lpReserved2', LPVOID),
                ('hStdInput', HANDLE), ('hStdOutput', HANDLE), ('hStdError', HANDLE)]


class StartupInfoEx(ctypes.Structure):
    _fields_ = [('StartupInfo', StartupInfo), ('lpAttributeList', LPVOID)]


class ProcessInformation(ctypes.Structure):
    _fields_ = [('hProcess', HANDLE), ('hThread', HANDLE), ('dwProcessId', DWORD), ('dwThreadId', DWORD)]


class WinAPI:
    def __init__(self, kernel32=None, get_last_error=None):
        self.dll = kernel32 if kernel32 is not None else ctypes.WinDLL('kernel32', use_last_error=True)
        self.get_last_error = get_last_error if get_last_error is not None else ctypes.get_last_error
        signatures = {
            'CreateJobObjectW': ([ctypes.POINTER(SecurityAttributes), LPWSTR], HANDLE),
            'SetInformationJobObject': ([HANDLE, ctypes.c_int32, LPVOID, DWORD], BOOL),
            'CreateFileW': ([LPWSTR, DWORD, DWORD, ctypes.POINTER(SecurityAttributes), DWORD, DWORD, HANDLE], HANDLE),
            'InitializeProcThreadAttributeList': ([LPVOID, DWORD, DWORD, ctypes.POINTER(SIZE_T)], BOOL),
            'UpdateProcThreadAttribute': ([LPVOID, DWORD, SIZE_T, LPVOID, SIZE_T, LPVOID, LPVOID], BOOL),
            'DeleteProcThreadAttributeList': ([LPVOID], None),
            'CreateProcessW': ([LPWSTR, LPWSTR, ctypes.POINTER(SecurityAttributes), ctypes.POINTER(SecurityAttributes),
                                BOOL, DWORD, LPVOID, LPWSTR, ctypes.POINTER(StartupInfoEx), ctypes.POINTER(ProcessInformation)], BOOL),
            'TerminateJobObject': ([HANDLE, DWORD], BOOL),
            'TerminateProcess': ([HANDLE, DWORD], BOOL),
            'WaitForSingleObject': ([HANDLE, DWORD], DWORD),
            'GetExitCodeProcess': ([HANDLE, ctypes.POINTER(DWORD)], BOOL),
            'CloseHandle': ([HANDLE], BOOL),
        }
        try:
            for name, (argtypes, restype) in signatures.items():
                fn = getattr(self.dll, name)
                fn.argtypes, fn.restype = argtypes, restype
                setattr(self, name, fn)
        except AttributeError as error:
            raise OSError('Windows process ownership requires Windows 10 or later') from error

    def error(self, operation):
        code = self.get_last_error()
        error = OSError(code, '%s failed (Windows error %d)' % (operation, code))
        error.winerror = code
        return error

    def check(self, result, operation):
        if not result:
            raise self.error(operation)
        return result

    def close_handle(self, handle):
        self.check(self.CloseHandle(handle), 'CloseHandle')


class WindowsJobProcess:
    """The small Popen interface needed by worker_gguf, with explicit close()."""
    def __init__(self, api, args):
        self._api, self.args = api, args
        self._job = self._handle = None
        self.pid = self.returncode = None

    @classmethod
    def start(cls, args, env, api=None, cwd=None):
        api = api if api is not None else WinAPI()
        proc = cls(api, args)
        nul = attributes = None
        initialized = False
        info = ProcessInformation()
        try:
            try:
                proc._job = api.check(api.CreateJobObjectW(None, None), 'CreateJobObjectW')
                limits = ExtendedLimitInformation()
                limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                api.check(api.SetInformationJobObject(proc._job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                                      ctypes.byref(limits), ctypes.sizeof(limits)), 'SetInformationJobObject')
                # Only NUL is inherited, never the job handle. A descendant must not
                # keep the job alive after the worker exits. No logs/text reach disk.
                security = SecurityAttributes(ctypes.sizeof(SecurityAttributes), None, True)
                nul = api.CreateFileW('NUL', 0x80000000 | 0x40000000, 3, ctypes.byref(security), 3, 0, None)
                if nul == INVALID_HANDLE_VALUE or nul is None:
                    nul = None
                    raise api.error('CreateFileW(NUL)')
                size = SIZE_T()
                ok = api.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
                if ok or api.get_last_error() != ERROR_INSUFFICIENT_BUFFER or not size.value:
                    raise api.error('InitializeProcThreadAttributeList(size)')
                attributes = ctypes.create_string_buffer(size.value)
                api.check(api.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)),
                          'InitializeProcThreadAttributeList')
                initialized = True
                handles, jobs = (HANDLE * 1)(nul), (HANDLE * 1)(proc._job)
                for name, value, label in ((PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles, 'HANDLE_LIST'),
                                           (PROC_THREAD_ATTRIBUTE_JOB_LIST, jobs, 'JOB_LIST')):
                    api.check(api.UpdateProcThreadAttribute(attributes, 0, name, value, ctypes.sizeof(value), None, None),
                              'UpdateProcThreadAttribute(%s)' % label)
                startup = StartupInfoEx()
                startup.StartupInfo.cb = ctypes.sizeof(startup)
                startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
                startup.StartupInfo.hStdInput = startup.StartupInfo.hStdOutput = startup.StartupInfo.hStdError = nul
                startup.lpAttributeList = ctypes.cast(attributes, LPVOID)
                command = ctypes.create_unicode_buffer(subprocess.list2cmdline(args))
                environment = ctypes.create_unicode_buffer('\0'.join('%s=%s' % (k, v) for k, v in
                                                         sorted(env.items(), key=lambda item: item[0].upper())) + '\0\0')
                # JOB_LIST is applied by the OS before the child can run. If it
                # cannot be applied, CreateProcessW fails instead of launching it.
                api.check(api.CreateProcessW(args[0], command, None, None, True,
                                             CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT,
                                             environment, cwd, ctypes.byref(startup), ctypes.byref(info)), 'CreateProcessW')
                proc._handle, proc.pid = info.hProcess, info.dwProcessId
            finally:
                # Try every temporary cleanup even when another one fails.
                errors = []
                if initialized:
                    api.DeleteProcThreadAttributeList(attributes)
                for handle in (info.hThread, nul):
                    if handle is not None:
                        try:
                            api.close_handle(handle)
                        except OSError as error:
                            errors.append(error)
                if errors:
                    raise errors[0]
        except BaseException:
            # An asynchronous exception can arrive just after CreateProcessW
            # populated its output, before the Python assignment above ran.
            if proc._handle is None and info.hProcess is not None:
                proc._handle, proc.pid = info.hProcess, info.dwProcessId
            # Also covers interruption or cleanup failure after successful spawn.
            # The job was already attached atomically, so closing it kills the tree.
            proc.close()
            raise
        return proc

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if self._handle is None:
            # Unknown after a failed close/wait is not evidence of liveness.
            raise OSError('The server process handle is closed; its state is unknown')
        result = self._api.WaitForSingleObject(self._handle, 0)
        if result == WAIT_TIMEOUT:
            return None
        if result != WAIT_OBJECT_0:
            raise self._api.error('WaitForSingleObject')
        code = DWORD()
        self._api.check(self._api.GetExitCodeProcess(self._handle, ctypes.byref(code)), 'GetExitCodeProcess')
        self.returncode = code.value
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is not None:
            return self.returncode
        if self._handle is None:
            raise OSError('The server process handle is closed; its state is unknown')
        milliseconds = INFINITE if timeout is None else min(INFINITE - 1, max(0, math.ceil(timeout * 1000)))
        result = self._api.WaitForSingleObject(self._handle, milliseconds)
        if result == WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(self.args, timeout)
        if result != WAIT_OBJECT_0:
            raise self._api.error('WaitForSingleObject')
        return self.poll()

    def terminate(self):
        # Terminating the whole job also cleans descendants after an early server exit.
        if self._job is not None:
            self._api.check(self._api.TerminateJobObject(self._job, 1), 'TerminateJobObject')

    kill = terminate

    def close(self):
        """Close the job (kills its tree), reap the process and release our handles."""
        errors = []
        if self._job is not None:
            try:
                self._api.close_handle(self._job)
                self._job = None
            except OSError as error:
                errors.append(error)
                # A failed CloseHandle must not turn a launch failure into an
                # unowned running server. Retain the handle for a later close.
                try:
                    self.terminate()
                except OSError as error:
                    errors.append(error)
                    if self._handle is not None:
                        try:
                            self._api.check(self._api.TerminateProcess(self._handle, 1), 'TerminateProcess')
                        except OSError as error:
                            errors.append(error)
        if self._handle is not None:
            try:
                self.wait(timeout=8)
            except (OSError, subprocess.TimeoutExpired) as error:
                errors.append(error)
            finally:
                try:
                    self._api.close_handle(self._handle)
                    self._handle = None
                except OSError as error:
                    errors.append(error)
        if errors:
            raise errors[0]
