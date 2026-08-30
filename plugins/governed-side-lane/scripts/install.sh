#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
mode=${1:-}
host=${2:-both}

usage() {
  echo "usage: $0 {install|check|uninstall} [codex|claude|both]" >&2
  exit 2
}

case "$mode" in
  install|check|uninstall) ;;
  *) usage ;;
esac
case "$host" in
  codex|claude|both) ;;
  *) usage ;;
esac
[[ $# -le 2 ]] || usage

runner_source="$repo_root/bin/side-lane"
runner_destination="$HOME/.local/bin/side-lane"
side_lane_source="$repo_root/skills/side-lane"
codex_destination="${CODEX_HOME:-$HOME/.codex}/skills/side-lane"
claude_destination="$HOME/.claude/skills/side-lane"
context_source="$repo_root/config/agent-context.md"
context_helper="$repo_root/scripts/context_entrypoint.py"
prompt_source="$repo_root/skills/prompt-it-side-lane-routing"
codex_prompt_destination="${CODEX_HOME:-$HOME/.codex}/skills/prompt-it-side-lane-routing"
claude_prompt_destination="$HOME/.claude/skills/prompt-it-side-lane-routing"

sources=("$runner_source")
destinations=("$runner_destination")
case "$host" in
  codex)
    sources+=("$side_lane_source" "$prompt_source")
    destinations+=("$codex_destination" "$codex_prompt_destination")
    ;;
  claude)
    sources+=("$side_lane_source" "$prompt_source")
    destinations+=("$claude_destination" "$claude_prompt_destination")
    ;;
  both)
    sources+=("$side_lane_source" "$prompt_source" "$side_lane_source" "$prompt_source")
    destinations+=("$codex_destination" "$codex_prompt_destination" "$claude_destination" "$claude_prompt_destination")
    ;;
esac

problems=0
changes=0

is_ours() {
  local source=$1 destination=$2
  [[ -L "$destination" && "$(readlink "$destination")" == "$source" ]]
}

remove_if_ours() {
  local source=$1 destination=$2 name=${2##*/}
  if is_ours "$source" "$destination"; then
    rm "$destination"
    echo "removed  $name"
    changes=$((changes + 1))
  elif [[ -e "$destination" || -L "$destination" ]]; then
    echo "skipped  $name: unrelated destination preserved"
  else
    echo "absent   $name"
  fi
}

if [[ "$mode" == install ]]; then
  for index in "${!sources[@]}"; do
    source=${sources[$index]}
    destination=${destinations[$index]}
    name=${destination##*/}
    if ! is_ours "$source" "$destination" && [[ -e "$destination" || -L "$destination" ]]; then
      echo "refusing $name: unrelated destination already exists" >&2
      problems=$((problems + 1))
    fi
  done
  if [[ $problems -ne 0 ]]; then
    echo "install stopped before making changes: $problems conflict(s)" >&2
    exit 1
  fi
fi

if [[ "$mode" == uninstall ]]; then
  case "$host" in
    codex)
      remove_if_ours "$side_lane_source" "$codex_destination"
      remove_if_ours "$prompt_source" "$codex_prompt_destination"
      ;;
    claude)
      remove_if_ours "$side_lane_source" "$claude_destination"
      remove_if_ours "$prompt_source" "$claude_prompt_destination"
      ;;
    both)
      remove_if_ours "$side_lane_source" "$codex_destination"
      remove_if_ours "$prompt_source" "$codex_prompt_destination"
      remove_if_ours "$side_lane_source" "$claude_destination"
      remove_if_ours "$prompt_source" "$claude_prompt_destination"
      ;;
  esac

  "$context_helper" uninstall "$host" --source "$context_source" --codex-home "${CODEX_HOME:-$HOME/.codex}" || problems=$((problems + 1))

  if [[ "$host" == codex ]] && is_ours "$side_lane_source" "$claude_destination"; then
    echo "kept     side-lane: still used by the Claude Code skill"
  elif [[ "$host" == claude ]] && is_ours "$side_lane_source" "$codex_destination"; then
    echo "kept     side-lane: still used by the Codex skill"
  else
    remove_if_ours "$runner_source" "$runner_destination"
  fi
  echo "$mode ($host) complete: $changes changed, $problems problem(s)"
  [[ $problems -eq 0 ]]
  exit
fi

for index in "${!sources[@]}"; do
  source=${sources[$index]}
  destination=${destinations[$index]}
  name=${destination##*/}

  case "$mode" in
    check)
      if is_ours "$source" "$destination"; then
        echo "ok       $name -> $source"
      elif [[ -e "$destination" || -L "$destination" ]]; then
        echo "conflict $name exists and is not managed by this checkout"
        problems=$((problems + 1))
      else
        echo "missing  $name"
        problems=$((problems + 1))
      fi
      ;;
    install)
      if is_ours "$source" "$destination"; then
        echo "ok       $name -> $source"
      else
        mkdir -p "$(dirname "$destination")"
        ln -s "$source" "$destination"
        echo "linked   $name -> $source"
        changes=$((changes + 1))
      fi
      ;;
  esac
done

"$context_helper" "$mode" "$host" --source "$context_source" --codex-home "${CODEX_HOME:-$HOME/.codex}" || problems=$((problems + 1))

case ":$PATH:" in
  *":$HOME/.local/bin:"*) echo "ok       $HOME/.local/bin is on PATH" ;;
  *) echo "note     $HOME/.local/bin is not on PATH" ;;
esac
echo "credential status (values are never displayed):"
"$repo_root/bin/side-lane" credentials || problems=$((problems + 1))

echo "$mode ($host) complete: $changes changed, $problems problem(s)"
[[ $problems -eq 0 ]]
