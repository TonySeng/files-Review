#!/usr/bin/env bash
# ============================================================
# 把部署包拷贝到 131 服务器并拉起（默认使用最新 final25 包）
# ------------------------------------------------------------
# 用法：
#   1) 在本机执行 ./make-deploy-package.sh 生成部署包（当前= final25）
#   2) 编辑下方 SERVER_IP / SERVER_USER（或执行时用环境变量传入）
#   3) 执行 ./transfer-to-131.sh
#
# 脚本会自动：scp 上传 → ssh 解压 → 进入目录 → 运行一键部署脚本。
# 部署方式默认用 deploy.sh（docker compose，需服务器已装 compose）；
# 若服务器只有 docker、没有 compose，把 DEPLOY_MODE 改为 docker。
# ============================================================
set -euo pipefail

# ====== 需填写 ======
SERVER_IP="${SERVER_IP:-}"          # 例如 192.168.x.131 或 10.x.x.131
SERVER_USER="${SERVER_USER:-root}"  # SSH 用户名
SSH_PORT="${SSH_PORT:-22}"
REMOTE_DIR="${REMOTE_DIR:-/opt}"    # 远端目标父目录（解压后位于 $REMOTE_DIR/bidding-review-deploy）
# ====================

TARBALL="${TARBALL:-bidding-review-arm64-final25.tar.gz}"
PKG_DIR="bidding-review-deploy"
DEPLOY_MODE="${DEPLOY_MODE:-compose}"   # compose | docker

if [ -z "$SERVER_IP" ]; then
  echo "错误：请在脚本顶部填写 SERVER_IP（131 服务器 IP）。" >&2
  exit 1
fi
if [ ! -f "$TARBALL" ]; then
  echo "错误：找不到 $TARBALL，请先运行 ./make-deploy-package.sh 生成。" >&2
  exit 1
fi

REMOTE="$( [ "$SSH_PORT" = "22" ] && echo "${SERVER_USER}@${SERVER_IP}" || echo -p "${SSH_PORT}" "${SERVER_USER}@${SERVER_IP}" )"

echo "=================================================="
echo " 部署到 131 服务器: ${SERVER_USER}@${SERVER_IP}"
echo " 包: $TARBALL"
echo "=================================================="

echo ">> [1/3] 上传部署包 ..."
scp -P "$SSH_PORT" "$TARBALL" "${SERVER_USER}@${SERVER_IP}:${REMOTE_DIR}/"

echo ">> [2/3] 解压 ..."
ssh -p "$SSH_PORT" "${SERVER_USER}@${SERVER_IP}" "cd ${REMOTE_DIR} && tar xzf ${TARBALL} && echo 解压完成"

echo ">> [3/3] 拉起服务（模式: $DEPLOY_MODE）..."
if [ "$DEPLOY_MODE" = "docker" ]; then
  ssh -p "$SSH_PORT" "${SERVER_USER}@${SERVER_IP}" "cd ${REMOTE_DIR}/${PKG_DIR} && chmod +x deploy-docker.sh && ./deploy-docker.sh"
else
  ssh -p "$SSH_PORT" "${SERVER_USER}@${SERVER_IP}" "cd ${REMOTE_DIR}/${PKG_DIR} && chmod +x deploy.sh && ./deploy.sh"
fi

echo ""
echo "=================================================="
echo " 部署完成！访问："
echo "   前端页面:  http://${SERVER_IP}:8180"
echo "   后端探活:  http://${SERVER_IP}:8100/api/health"
echo "=================================================="
