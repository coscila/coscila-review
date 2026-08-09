#!/usr/bin/env bash
set -euo pipefail

: "${OCSL_REVIEW_HOME:?OCSL_REVIEW_HOME is required}"
: "${OCSL_REVIEW_STATE:?OCSL_REVIEW_STATE is required}"

if [[ -n "${OCSL_REVIEW_MCP_PYTHON:-}" ]]; then
  python_bin="$OCSL_REVIEW_MCP_PYTHON"
else
  platform="${OCSL_REVIEW_PLATFORM:-}"
  if [[ -z "$platform" ]]; then
    case "$(uname -s):$(uname -m)" in
      Linux:x86_64) platform="linux-64" ;;
      Linux:aarch64|Linux:arm64) platform="linux-aarch64" ;;
      *) echo "unsupported MCP host platform" >&2; exit 78 ;;
    esac
  fi
  template_root="$OCSL_REVIEW_STATE/environment-templates/$platform/mcp-review"
  current="$template_root/current"
  if [[ ! -L "$current" ]]; then
    echo "mcp-review template is not promoted" >&2
    exit 78
  fi
  canonical_root="$(readlink -f -- "$template_root")"
  promoted="$(readlink -f -- "$current")"
  case "$promoted" in
    "$canonical_root"/*) ;;
    *) echo "mcp-review current pointer escapes its template root" >&2; exit 78 ;;
  esac
  if [[ ! -f "$promoted/.ocsl-review-template.json" || ! -x "$promoted/bin/python" ]]; then
    echo "mcp-review promoted template is incomplete" >&2
    exit 78
  fi
  python_bin="$promoted/bin/python"
fi

export PYTHONPATH="$OCSL_REVIEW_HOME/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$python_bin" -m ocsl_review.catalog_server
