#!/usr/bin/env bash
# Full evaluation: the remaining dev incidents, then every config on the held-out set.
# Episodes that already exist are skipped, so this can be rerun after an interruption (e.g. a usage limit).
set -u
export PYTHONIOENCODING=utf-8
export PIA_SANDBOX="${PIA_SANDBOX:-$(pwd)/sandbox/agent/state}"
uv run --no-sync pia eval run --set dev --config opus+sonnet || exit 1
for cfg in opus+sonnet opus-solo sonnet-only opus+sonnet-noimg; do
  uv run --no-sync pia eval run --set held-out --config "$cfg" || exit 1
done
