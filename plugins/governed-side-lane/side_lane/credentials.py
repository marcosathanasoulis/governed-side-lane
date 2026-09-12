from __future__ import annotations

import ctypes
from ctypes import wintypes
import getpass
import os
from pathlib import Path
import platform
import re
import subprocess
from typing import Mapping


class CredentialError(Exception):
    pass


ENV_BACKEND_FLAG = "SIDE_LANE_CREDENTIAL_BACKEND"
ENV_VALUE_PREFIX = "SIDE_LANE_CREDENTIAL_"
ENV_DIR_VAR = "SIDE_LANE_CREDENTIALS_DIR"


def _validated_service(service: str) -> str:
    if (not isinstance(service, str) or not service or service in {".", ".."}
            or "/" in service or "\\" in service or ":" in service or "\x00" in service):
        raise CredentialError("credential service must be a non-empty portable file name")
    return service


def env_variable_for_service(service: str) -> str:
    service = _validated_service(service)
    return ENV_VALUE_PREFIX + re.sub(r"[^A-Za-z0-9]", "_", service).upper()


def scrub_backend_environment(inherited: Mapping[str, str]) -> dict[str, str]:
    """Remove every parent-only credential backend variable from a child env."""

    return {
        name: value for name, value in inherited.items()
        if name != ENV_DIR_VAR and not name.startswith(ENV_VALUE_PREFIX)
    }


def _credential_file(service: str) -> Path | None:
    directory = os.environ.get(ENV_DIR_VAR)
    if not directory:
        return None
    root = Path(directory).expanduser().resolve()
    candidate = (root / _validated_service(service)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise CredentialError("credential service escapes the configured directory") from exc
    return candidate


def _env_read(service: str, *, reveal: bool) -> str | bool:
    service = _validated_service(service)
    value = os.environ.get(env_variable_for_service(service))
    if value is not None:
        if not value.strip():
            if reveal:
                raise CredentialError(f"credential absent for service {service}")
            return False
        return value.strip() if reveal else True
    path = _credential_file(service)
    if path is not None:
        if not reveal:
            try:
                return path.is_file() and path.stat().st_size > 0
            except OSError:
                return False
        try:
            value = path.read_text(encoding="utf-8") if path.is_file() else None
        except (OSError, UnicodeError) as exc:
            raise CredentialError(f"credential file lookup failed for service {service}") from exc
        if value is not None and value.strip():
            return value.strip()
    if reveal:
        raise CredentialError(f"credential absent for service {service}")
    return False


def _uses_env_backend(selected: str) -> bool:
    if os.environ.get(ENV_BACKEND_FLAG, "").strip().lower() == "env":
        return True
    return selected == "Linux"


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
    if _uses_env_backend(selected):
        return bool(_env_read(service, reveal=False))
    if selected == "Darwin":
        return bool(_macos_read(service, reveal=False))
    if selected == "Windows":
        return bool(_windows_read(service, reveal=False))
    return False


def read_credential(service: str, system: str | None = None) -> str:
    selected = system or platform.system()
    if _uses_env_backend(selected):
        return str(_env_read(service, reveal=True))
    if selected == "Darwin":
        return str(_macos_read(service, reveal=True))
    if selected == "Windows":
        return str(_windows_read(service, reveal=True))
    raise CredentialError(
        "supported credential stores are macOS Keychain, Windows Credential Manager, "
        "and the env/file backend (SIDE_LANE_CREDENTIAL_BACKEND=env or Linux)"
    )
