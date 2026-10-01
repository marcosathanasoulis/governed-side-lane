from __future__ import annotations

import ctypes
from ctypes import wintypes
import getpass
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
from typing import Callable, Mapping


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


#: Services the interactive setup wizard (``scripts/setup.py``) is allowed to
#: write to or delete. A write path is far more consequential than a read:
#: an unbounded service name would let a caller plant an arbitrary Keychain
#: item or `secret-tool` collection entry under this process's identity. The
#: allowlist is intentionally small and explicit rather than derived from
#: ``config/models.json`` at call time, so a future config edit cannot
#: silently widen what this module will write.
WRITABLE_SERVICES = frozenset({"governed-side-lane-openrouter", "governed-side-lane-glm"})


def _validated_writable_service(service: str) -> str:
    service = _validated_service(service)
    if service not in WRITABLE_SERVICES:
        raise CredentialError(
            f"credential service {service!r} is not in the writable allowlist "
            f"({', '.join(sorted(WRITABLE_SERVICES))})"
        )
    return service


def _security_quote(value: str) -> str:
    """Escape one value for ``security -i``'s quoted-string keychain script syntax.

    The interactive command language (``man security``) takes double-quoted
    string arguments and supports backslash escaping inside them; backslash
    and double-quote are escaped here so an arbitrary secret value cannot
    terminate the quoted string early or otherwise alter the script.
    """

    return value.replace("\\", "\\\\").replace('"', '\\"')


def _macos_store(service: str, secret: str) -> None:
    """Add/replace one Keychain generic-password item; the secret never touches argv.

    ``security add-generic-password -w <password>`` on the ordinary command
    line puts the password on this process's argv, visible to any local
    process listing. ``security -i`` instead reads a keychain-scripting
    command from stdin and runs it as its own interactive session, so the
    secret travels only through the pipe, never through argv, an environment
    variable, or a file. ``-U`` updates an existing item in place rather than
    requiring a delete-then-add race.
    """

    script = (
        f'add-generic-password -U -a "{_security_quote(getpass.getuser())}" '
        f'-s "{_security_quote(service)}" -w "{_security_quote(secret)}"\n'
        "quit\n"
    )
    completed = subprocess.run(
        ["security", "-i"],
        input=script,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise CredentialError(f"could not store credential for service {service}")


def _macos_delete(service: str) -> None:
    completed = subprocess.run(
        ["security", "delete-generic-password", "-a", getpass.getuser(), "-s", service],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if completed.returncode not in (0, 44):
        raise CredentialError(f"could not delete credential for service {service}")


def _windows_credential_script() -> Path:
    return Path(__file__).resolve().parents[1] / "scripts" / "credential.ps1"


def _windows_store(service: str, secret: str) -> None:
    """Delegate to ``scripts/credential.ps1``, which reads the secret as a SecureString.

    The secret is written to the child's stdin, one line, immediately
    followed by closing stdin; PowerShell's ``Read-Host -AsSecureString``
    reads exactly one line and never echoes it, and it never appears in this
    process's argv or in any log this module writes.
    """

    script = _windows_credential_script()
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-File", str(script), "set", service],
        input=secret + "\n",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise CredentialError(f"could not store credential for service {service}")


def _windows_delete(service: str) -> None:
    script = _windows_credential_script()
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-File", str(script), "delete", service],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise CredentialError(f"could not delete credential for service {service}")


def _linux_secret_tool_available(which: "Callable[[str], str | None] | None" = None) -> bool:
    lookup = which or shutil.which
    return lookup("secret-tool") is not None


def _linux_store(service: str, secret: str, *, which=None) -> None:
    """Store via ``secret-tool store`` (GNOME Keyring / Secret Service).

    The secret is piped to the child's stdin (``secret-tool store`` reads the
    password from stdin, never from argv), and the lookup attributes name the
    service and the OS user so ``credential_present``/``read_credential``'s
    own ``secret-tool lookup`` (reached through the ``Linux`` env/file
    backend path only when explicitly selected) can find it again. There is
    no plaintext fallback: when ``secret-tool`` is unavailable this raises
    rather than ever writing the secret to an ordinary file.
    """

    if not _linux_secret_tool_available(which):
        raise CredentialError(
            "no Secret Service provider is available (secret-tool not found on "
            "PATH); there is no plaintext fallback. Install a Secret Service "
            "provider (for example gnome-keyring) and retry, or use the "
            "SIDE_LANE_CREDENTIALS_DIR file backend explicitly"
        )
    completed = subprocess.run(
        ["secret-tool", "store", "--label", f"side-lane: {service}",
         "service", service, "account", getpass.getuser()],
        input=secret + "\n",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise CredentialError(f"could not store credential for service {service}")


def _linux_delete(service: str, *, which=None) -> None:
    if not _linux_secret_tool_available(which):
        raise CredentialError(
            "no Secret Service provider is available (secret-tool not found on PATH)"
        )
    completed = subprocess.run(
        ["secret-tool", "clear", "service", service, "account", getpass.getuser()],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if completed.returncode not in (0, 1):
        # secret-tool clear exits 1 when nothing matched; that is not a
        # failure for an idempotent delete.
        raise CredentialError(f"could not delete credential for service {service}")


def store_credential(service: str, secret: str, system: str | None = None) -> None:
    """Store one secret for an allowlisted service, never via argv or a log.

    Supported stores only: macOS Keychain, Windows Credential Manager (via
    ``scripts/credential.ps1``), and Linux Secret Service (via
    ``secret-tool``). There is no plaintext-file fallback for a write: a
    platform or environment without one of these refuses rather than ever
    writing a secret to an ordinary file.
    """

    service = _validated_writable_service(service)
    if not isinstance(secret, str) or not secret.strip():
        raise CredentialError("credential value must be a non-empty string")
    if any(ord(char) < 32 or ord(char) == 127 for char in secret):
        raise CredentialError("credential value must not contain control characters")
    selected = system or platform.system()
    if selected == "Darwin":
        return _macos_store(service, secret)
    if selected == "Windows":
        return _windows_store(service, secret)
    if selected == "Linux":
        return _linux_store(service, secret)
    raise CredentialError(
        "supported credential stores are macOS Keychain, Windows Credential "
        "Manager, and Linux Secret Service (secret-tool); there is no "
        "plaintext fallback"
    )


def delete_credential(service: str, system: str | None = None) -> None:
    """Delete one allowlisted service's stored credential; idempotent."""

    service = _validated_writable_service(service)
    selected = system or platform.system()
    if selected == "Darwin":
        return _macos_delete(service)
    if selected == "Windows":
        return _windows_delete(service)
    if selected == "Linux":
        return _linux_delete(service)
    raise CredentialError(
        "supported credential stores are macOS Keychain, Windows Credential "
        "Manager, and Linux Secret Service (secret-tool)"
    )
