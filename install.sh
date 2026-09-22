#!/usr/bin/env bash
# Bootstrap only the two configuration dependencies; analyzers use the consumer venv.
set -euo pipefail
tool_dir="$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")"
project="$PWD"
args=("$@")
for ((index=0; index<${#args[@]}; index++)); do
  if [[ "${args[$index]}" == --project ]]; then
    index=$((index+1))
    if (( index >= ${#args[@]} )); then
      printf '%s\n' 'ERROR: --project needs a directory' >&2
      exit 2
    fi
    project="${args[$index]}"
  elif [[ "${args[$index]}" == --project=* ]]; then
    project="${args[$index]#--project=}"
  fi
done
if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' 'ERROR: uv must be installed' >&2
  exit 2
fi
export PYTHONDONTWRITEBYTECODE=1 UV_NO_PROGRESS=1
project="$(realpath -- "$project")"
uv --directory "$project" run --no-project --python '>=3.12' \
  --with 'tomlkit==0.13.3' --with 'packaging==25.0' \
  python "$tool_dir/install.py" "${args[@]}" --project "$project" || exit 2
