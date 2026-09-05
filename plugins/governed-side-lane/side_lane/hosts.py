"""Locate native host executables explicitly, without silent substitution.

The Codex and Claude CLIs are normally found on ``PATH``. The Codex CLI is
also shipped inside the Codex / ChatGPT desktop app bundle on macOS, where a
non-interactive shell (an agent's Bash tool, ``zsh -c``) frequently cannot see
it even though the desktop app is signed in and working. This module makes the
lookup order explicit and reports the exact executable that will run:

1. ``SIDE_LANE_<HOST>_EXECUTABLE`` — an explicit override. If it is set but is
   not an executable file the lookup fails closed instead of falling back.
2. ``shutil.which(host)`` — the conventional ``PATH`` lookup.
3. Known desktop-app bundle locations (Codex only).

Nothing here reads, stores, or prints credentials.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Callable, Mapping

SUPPORTED_HOSTS = frozenset({"codex", "claude"})

EXECUTABLE_ENV = {
    "codex": "SIDE_LANE_CODEX_EXECUTABLE",
    "claude": "SIDE_LANE_CLAUDE_EXECUTABLE",
}

# Desktop-app bundles that ship the Codex CLI on macOS. Later entries are only
# consulted when earlier ones are absent; the resolved path is always reported.
BUNDLED_CODEX_CANDIDATES: tuple[str, ...] = (
    "/Applications/Codex.app/Contents/Resources/codex",
    "~/Applications/Codex.app/Contents/Resources/codex",
    "/Applications/ChatGPT.app/Contents/Resources/codex",
    "~/Applications/ChatGPT.app/Contents/Resources/codex",
)

# Helper binaries a host spawns from its own directory. codex-cli 0.152+ launches
# ``codex-code-mode-host`` as a sibling of the ``codex`` executable; when the
# CLI is reached through a symlink elsewhere the sibling lookup fails. The
# lane exports the real directory on the child's ``PATH`` so the host finds it.
HOST_SUPPORT_BINARIES = {
    "codex": ("codex-code-mode-host",),
    "claude": (),
}

INSTALL_HINTS = {
    "codex": "install the Codex CLI (`npm install -g @openai/codex`), or the Codex desktop app",
    "claude": "install Claude Code (`npm install -g @anthropic-ai/claude-code`)",
}

Which = Callable[[str], "str | None"]


class HostExecutableError(RuntimeError):
    """A native host executable could not be located or an override is invalid."""


def _absolute(path: Path) -> Path:
    """Anchor a path to the current directory without resolving symlinks."""

    return Path(os.path.abspath(path))


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def resolve_host_executable(
    host: str,
    *,
    env: Mapping[str, str] | None = None,
    which: Which = shutil.which,
) -> str | None:
    """Return the absolute executable for ``host`` or ``None`` when absent.

    The result is always absolute (relative overrides and relative ``PATH``
    entries are anchored to the current directory once, here) so later steps
    never depend on the working directory. Symlinks are preserved so the
    reported path is the one the operator installed.

    Raises ``HostExecutableError`` when an explicit override is set but unusable,
    so a misconfigured override never degrades into a different binary.
    """

    if host not in SUPPORTED_HOSTS:
        raise HostExecutableError(f"unsupported native host: {host}")
    environment = os.environ if env is None else env
    override = environment.get(EXECUTABLE_ENV[host], "")
    if override:
        candidate = _absolute(Path(override).expanduser())
        if not _is_executable_file(candidate):
            raise HostExecutableError(
                f"{EXECUTABLE_ENV[host]} is set but is not an executable file: {candidate}"
            )
        return str(candidate)
    found = which(host)
    if found:
        return str(_absolute(Path(found)))
    if host == "codex":
        for raw in BUNDLED_CODEX_CANDIDATES:
            candidate = _absolute(Path(raw).expanduser())
            if _is_executable_file(candidate):
                return str(candidate)
    return None


def require_host_executable(
    host: str,
    *,
    env: Mapping[str, str] | None = None,
    which: Which = shutil.which,
) -> str:
    """Return the executable for ``host`` or raise an actionable error."""

    executable = resolve_host_executable(host, env=env, which=which)
    if executable:
        return executable
    raise HostExecutableError(
        f"{host} executable not found on PATH"
        + (" or in a Codex/ChatGPT desktop app bundle" if host == "codex" else "")
        + f"; {INSTALL_HINTS[host]}, or set {EXECUTABLE_ENV[host]} to its absolute path"
    )


def host_support_dir(host: str, executable: str | None) -> str | None:
    """Return the real directory of ``executable`` when it holds the host's helpers.

    Symlinks are resolved here (and only here) because the helper lives next to
    the real binary, not next to the operator's symlink. ``None`` means no helper
    is needed or none was found; nothing is substituted in either case.
    """

    if host not in SUPPORTED_HOSTS:
        raise HostExecutableError(f"unsupported native host: {host}")
    if not executable or not HOST_SUPPORT_BINARIES[host]:
        return None
    try:
        real_dir = Path(executable).expanduser().resolve(strict=True).parent
    except OSError:
        return None
    if all(_is_executable_file(real_dir / name) for name in HOST_SUPPORT_BINARIES[host]):
        return str(real_dir)
    return None


def with_support_dir(env: Mapping[str, str], support_dir: str | None) -> dict[str, str]:
    """Prepend ``support_dir`` to ``PATH`` exactly once; never reorder otherwise."""

    child = dict(env)
    if not support_dir:
        return child
    entries = [entry for entry in child.get("PATH", "").split(os.pathsep) if entry]
    if entries[:1] == [support_dir]:
        return child
    child["PATH"] = os.pathsep.join([support_dir, *(entry for entry in entries if entry != support_dir)])
    return child
