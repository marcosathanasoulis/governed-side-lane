from __future__ import annotations

import ctypes
from ctypes import wintypes
import getpass
import platform
import subprocess


class CredentialError(Exception):
    pass


def _macos_read(service: str, *, reveal: bool) -> str | bool:
    command = ["security", "find-generic-password", "-a", getpass.getuser(), "-s", service]
    if reveal:
        command.append("-w")
    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if reveal else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if result.returncode == 44:
        if reveal:
            raise CredentialError(f"credential absent for service {service}")
        return False
    if result.returncode != 0:
        raise CredentialError(f"credential store lookup failed for service {service}")
    if not reveal:
        return True
    value = result.stdout.rstrip("\r\n")
    if not value:
        raise CredentialError(f"credential absent for service {service}")
    return value


def _windows_read(service: str, *, reveal: bool) -> str | bool:
    if not hasattr(ctypes, "windll"):
        raise CredentialError("Windows Credential Manager is unavailable")

    class Credential(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
        ]

    pointer = ctypes.POINTER(Credential)()
    ok = ctypes.windll.advapi32.CredReadW(service, 1, 0, ctypes.byref(pointer))
    if not ok:
        error = ctypes.windll.kernel32.GetLastError()
        if error == 1168:  # ERROR_NOT_FOUND
            if reveal:
                raise CredentialError(f"credential absent for service {service}")
            return False
        raise CredentialError(f"credential store lookup failed for service {service}")
    try:
        if not reveal:
            return True
        item = pointer.contents
        value = ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize).decode("utf-16-le")
        if not value:
            raise CredentialError(f"credential absent for service {service}")
        return value
    finally:
        ctypes.windll.advapi32.CredFree(pointer)


def credential_present(service: str, system: str | None = None) -> bool:
    selected = system or platform.system()
    if selected == "Darwin":
        return bool(_macos_read(service, reveal=False))
    if selected == "Windows":
        return bool(_windows_read(service, reveal=False))
    return False


def read_credential(service: str, system: str | None = None) -> str:
    selected = system or platform.system()
    if selected == "Darwin":
        return str(_macos_read(service, reveal=True))
    if selected == "Windows":
        return str(_windows_read(service, reveal=True))
    raise CredentialError("supported credential stores are macOS Keychain and Windows Credential Manager")
