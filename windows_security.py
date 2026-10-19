"""Windows user-scoped DPAPI and private directories. No portable key fallback."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path


class ProtectionError(RuntimeError):
    pass


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _windows():
    if os.name != 'nt':
        raise ProtectionError('Camera credentials require Windows user protection.')


def _crypt(data, decrypt=False):
    _windows()
    api = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel.LocalFree.restype = ctypes.c_void_p
    source = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    fn = api.CryptUnprotectData if decrypt else api.CryptProtectData
    fn.argtypes = (ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob))
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ProtectionError('Camera credentials cannot be unlocked by this Windows profile.')
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel.LocalFree(outgoing.data)


def protect(data):
    # USER scope: deliberately never set CRYPTPROTECT_LOCAL_MACHINE.
    return _crypt(data)


def unprotect(data):
    return _crypt(data, decrypt=True)


def private_directory(directory):
    """Apply an inheritable, protected DACL for the current user and SYSTEM."""
    _windows()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    adv.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    adv.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    adv.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
    adv.GetSecurityDescriptorDacl.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                                            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL))
    adv.SetNamedSecurityInfoW.argtypes = (wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                                        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    adv.SetNamedSecurityInfoW.restype = wintypes.DWORD
    token, sid_text, descriptor = wintypes.HANDLE(), wintypes.LPWSTR(), ctypes.c_void_p()
    try:
        if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise OSError
        size = wintypes.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise OSError
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        if not adv.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise OSError
        sddl = 'D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;' + sid_text.value + ')'
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise OSError
        present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
        if not adv.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
            raise OSError
        if adv.SetNamedSecurityInfoW(str(directory), 1, 0x80000004, None, None, dacl, None):
            raise OSError
    except OSError:
        raise ProtectionError('Unable to restrict access to the camera data directory.') from None
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)
        if sid_text:
            kernel.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        if token:
            kernel.CloseHandle(token)
    return directory
