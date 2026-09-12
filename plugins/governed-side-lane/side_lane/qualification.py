"""Explicit local candidate trials. Never activates a production route.

This module is an operational entrypoint, not an automated test. Callers must
supply recorded user authority, an exact successful transport probe, a dedicated
worktree and a bounded task. Model requests and secret retrieval are forbidden
in automated validation; tests inject a fake runner and fake secret.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
from typing import Any, Mapping

from side_lane.adapters import claude


def bounded_process(command, *, timeout, **kwargs):
    """Cancel the worker process group so a timeout does not leave paid work."""
    process = subprocess.Popen(command, start_new_session=True, **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        if isinstance(exc, KeyboardInterrupt):
            raise
        return subprocess.CompletedProcess(command, 124, stdout,
            (stderr or "") + "\nqualification timed out; worker process group stopped")
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def check_auth_overrides(paths):
    """Reject conflicting saved auth instead of putting a secret in argv/files."""
    for path in paths:
        if not path.is_file():
            continue
        data = json.loads(path.read_text())
        env = data.get("env", {})
        if data.get("apiKeyHelper") or any(env.get(name) for name in (
            "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
        )):
            raise ValueError("saved auth override must be resolved before qualification")


def qualify_claude(*, executable: str, repo: Path, worktree: Path,
                   provider: str, model: str, endpoint: str, secret: str,
                   transport_probe: Mapping[str, Any], prompt: str,
                   approved: bool = False, timeout: int = 180,
                   runner=bounded_process) -> dict[str, Any]:
    if not approved:
        raise ValueError("explicit paid local trial authority required")
    if provider not in claude.FIRST_WAVE_ENDPOINTS:
        raise ValueError("provider not supported by qualification harness")
    if not 1 <= timeout <= 300:
        raise ValueError("qualification timeout must be 1..300 seconds")
    if any(transport_probe.get(k) != v for k, v in {
        "provider": provider, "requested_model": model, "resolved_model": model,
        "endpoint": endpoint, "http_status": 200, "ready": True,
    }.items()):
        raise ValueError("exact successful transport probe required")
    paths = [Path.home()/'.claude/settings.json', Path.home()/'.claude/settings.local.json',
             Path('/Library/Application Support/ClaudeCode/managed-settings.json')]
    for parent in (repo, worktree):
        paths.extend([parent/'.claude/settings.json', parent/'.claude/settings.local.json'])
    check_auth_overrides(paths)
    pc = {"gateway": "direct-"+provider, "auth_method": "provider-key",
          "billable": True, "base_url": endpoint}
    mc = {"runtime_model": model, "protocol": "anthropic-compatible",
          "identity_contract": {"requested_model": model, "resolved_model": model,
                                "settings_precedence": "verified"},
          "max_budget_usd": 1}
    command = claude.build_command(executable=executable, repo=repo, worktree=worktree,
        provider=provider, model=model, provider_config=pc, model_config=mc,
        prompt=prompt, capabilities=("shell",))
    command[command.index('--output-format')+1] = 'json'
    command.append('--no-session-persistence')
    env = claude.build_transport_environment(os.environ, provider=provider, model=model,
        provider_config=pc, model_config=mc, mode='execute', secret=secret)
    env.pop('CLAUDE_CODE_OAUTH_TOKEN', None)
    env['CLAUDE_CODE_MAX_OUTPUT_TOKENS'] = '4096'
    env['MAX_THINKING_TOKENS'] = '1024'
    completed = runner(command, timeout=timeout, cwd=worktree, env=env,
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True)
    stdout = (completed.stdout or '').replace(secret, '[REDACTED]')
    stderr = (completed.stderr or '').replace(secret, '[REDACTED]')
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        result = {"parse_error": True, "text": stdout[:2000]}
    return {"qualification_only": True, "activated": False, "provider": provider,
            "requested_model": model, "endpoint": endpoint, "exit_status": completed.returncode,
            "worktree": str(worktree), "result": result, "stderr": stderr[:2000]}
