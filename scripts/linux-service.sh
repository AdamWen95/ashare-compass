#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
project_python="$project_root/.venv/bin/python"
if [[ ! -x "$project_python" ]]; then
    printf '%s\n' '请先在项目目录建立 Python 3.12 的 .venv；本脚本不修改系统 Python。' >&2
    exit 2
fi
exec "$project_python" "$project_root/scripts/linux_services.py" "$@"
