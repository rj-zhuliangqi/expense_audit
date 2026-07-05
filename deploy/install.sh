#!/usr/bin/env bash
# Expense Audit 服务器部署脚本
# 用法：在服务器上 git clone 后执行  bash deploy/install.sh
# 幂等：可重复执行。
set -euo pipefail

# ---------- 配置（按需修改）----------
PYTHON_BIN="${PYTHON_BIN:-python3}"          # 服务器 Python 解释器，要求 >= 3.12
DEPLOY_USER="${DEPLOY_USER:-$(whoami)}"      # systemd unit 运行用户
# -------------------------------------

# 定位项目根：本脚本在 deploy/ 下，根在上一级
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo "================ Expense Audit 部署 ================"
echo "项目根:        $PROJECT_ROOT"
echo "Python:        $($PYTHON_BIN --version 2>&1)"
echo "运行用户:      $DEPLOY_USER"
echo "----------------------------------------------------"

# 1. 校验 Python 版本 >= 3.12
py_major_minor="$($PYTHON_BIN -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
py_ok="$(awk -v v="$py_major_minor" 'BEGIN{split(v,a,".");print (a[1]>3 || (a[1]==3 && a[2]>=12)) ? 1 : 0}')"
if [ "$py_ok" != "1" ]; then
  echo "✗ Python 版本 $py_major_minor 不满足要求（>= 3.12）。用 PYTHON_BIN=/path/to/python3.12 重新执行。" >&2
  exit 1
fi

# 2. 校验 .env 存在
if [ ! -f "$PROJECT_ROOT/.env" ]; then
  if [ -f "$PROJECT_ROOT/.env.example" ]; then
    echo "✗ 未找到 .env。请先复制并填写： cp .env.example .env && vim .env" >&2
  else
    echo "✗ 未找到 .env。" >&2
  fi
  exit 1
fi

# 3. 创建虚拟环境并装依赖
if [ ! -x "$PROJECT_ROOT/.venv/bin/python" ]; then
  echo "→ 创建虚拟环境 .venv"
  "$PYTHON_BIN" -m venv "$PROJECT_ROOT/.venv"
fi
echo "→ 安装依赖（requirements.txt 已锁版本）"
"$PROJECT_ROOT/.venv/bin/python" -m pip install --upgrade pip >/dev/null
"$PROJECT_ROOT/.venv/bin/python" -m pip install -r "$PROJECT_ROOT/requirements.txt"

# 4. 渲染 systemd unit 模板（替换 {{DEPLOY_USER}} / {{DEPLOY_DIR}}）
echo "→ 渲染 systemd unit"
UNIT_DIR="$PROJECT_ROOT/deploy/systemd-units"
mkdir -p "$UNIT_DIR"
for tmpl in "$PROJECT_ROOT"/deploy/*.service; do
  unit_name="$(basename "$tmpl")"
  out="$UNIT_DIR/$unit_name"
  sed -e "s|{{DEPLOY_USER}}|$DEPLOY_USER|g" \
      -e "s|{{DEPLOY_DIR}}|$PROJECT_ROOT|g" \
      "$tmpl" > "$out"
  echo "   生成 $out"
done

echo ""
echo "================ 依赖与 unit 就绪 ================"
echo "下一步（需 sudo）安装并启动服务："
echo ""
echo "  sudo cp $UNIT_DIR/*.service /etc/systemd/system/"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable --now expense-graph-runtime expense-node-gateway expense-rabbitmq-worker"
echo ""
echo "查看状态：  systemctl status expense-graph-runtime expense-node-gateway expense-rabbitmq-worker"
echo "查看日志：  journalctl -u expense-rabbitmq-worker -f"
echo ""
echo "⚠ 三个服务的启动顺序：graph-runtime 与 node-gateway 先起，worker 后起。"
echo "  worker 启动会校验 AUDIT_SERVICE_URL 不指向 127.0.0.1/localhost，确认 .env 已填真实网关。"
