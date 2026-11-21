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

if [ ! -f .env ]; then
  cp .env.example .env
  echo "已生成 .env，可按需修改后重新运行 ./start.sh"
fi

"${compose[@]}" up -d --build

echo -n "等待服务就绪"
for _ in $(seq 1 30); do
  status=$(docker inspect -f '{{.State.Health.Status}}' mailbox 2>/dev/null || echo starting)
  [ "$status" = healthy ] && break
  echo -n "."
  sleep 1
done
echo

if [ "$status" != healthy ]; then
  echo "服务未能正常启动，最近的日志：" >&2
  "${compose[@]}" logs --tail 30 mailbox >&2
  exit 1
fi

http_port=$(grep -E '^HTTP_PORT=' .env | tail -1 | cut -d= -f2)
echo "已启动"
echo "看板地址：http://127.0.0.1:${http_port:-8080}"
if grep -qE '^API_KEY=.+' .env; then
  echo "API Key ：见 .env 中的 API_KEY"
else
  echo "API Key ：$("${compose[@]}" exec -T mailbox cat /data/api_key)"
fi
