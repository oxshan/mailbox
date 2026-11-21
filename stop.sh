#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if docker compose version >/dev/null 2>&1; then
  compose=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  compose=(docker-compose)
else
  echo "未找到 docker compose，请先安装 Docker" >&2
  exit 1
fi

"${compose[@]}" down
echo "已停止。邮件和配置保存在数据卷 mailbox-data 中，下次启动会继续使用。"
