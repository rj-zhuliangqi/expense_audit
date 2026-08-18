# Expense Audit 部署说明

生产环境只部署已经合并到 `main` 的提交。`deploy/templates/` 中的文件是可版本管理的 systemd 模板，`deploy/systemd-units/` 是本机渲染产物，默认不提交。

## 前置条件

- Linux、Python >= 3.12、sudo 权限
- 服务器可访问金蝶 OCR、真实审单网关、RabbitMQ 和 LLM API
- 已配置项目根目录 `.env`（仓库只提供 `.env.example`）

## 部署步骤

```bash
git clone https://github.com/rj-zhuliangqi/expense_audit.git
cd expense_audit
git switch main
cp .env.example .env
vim .env
bash deploy/install.sh

sudo cp deploy/systemd-units/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now \
  expense-graph-runtime \
  expense-node-gateway \
  expense-rabbitmq-worker
```

`deploy/install.sh` 会从 `deploy/templates/` 渲染 unit，并将 `{{DEPLOY_USER}}`、`{{DEPLOY_DIR}}` 替换为当前部署环境。不要手工提交或复制 `deploy/systemd-units/` 中的本机绝对路径。

## 服务与配置

| 服务 | 默认监听地址 | 默认端口 | 说明 |
|------|-------------|---------|------|
| `expense-graph-runtime` | `127.0.0.1` | `8090` | GoRules 图运行时 |
| `expense-node-gateway` | `127.0.0.1` | `8091` | 图内 LLM/节点调用网关 |
| `expense-rabbitmq-worker` | — | — | RabbitMQ → 数据准备 → runtime → 回写 |

监听地址和端口由 `.env` 中的 `GRAPH_RUNTIME_HOST`、`GRAPH_RUNTIME_PORT`、`NODE_GATEWAY_HOST`、`NODE_GATEWAY_PORT` 提供；模板仍保留 `127.0.0.1:8090` 和 `127.0.0.1:8091` 的默认值，不改变现有 systemd 服务名称、接口路径或生产端口约定。worker 使用 `GRAPH_RUNTIME_URL` 和 `AUDIT_SERVICE_URL`，费用类型图路径使用 `resources/graphs/` 下的四个正式流程图。

启动顺序：graph runtime 和 node gateway 先启动，worker 后启动。worker 会拒绝指向本地 mock 的 `AUDIT_SERVICE_URL`。

## 常用运维命令

```bash
systemctl status expense-graph-runtime expense-node-gateway expense-rabbitmq-worker
journalctl -u expense-rabbitmq-worker -f
sudo systemctl restart expense-rabbitmq-worker

# 更新已合并到 main 的代码后重新渲染并重启
git switch main
git pull --ff-only origin main
bash deploy/install.sh
sudo cp deploy/systemd-units/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart expense-graph-runtime expense-node-gateway expense-rabbitmq-worker
```

## 流程图与运行入口

worker 的默认图是 `resources/graphs/graph-latest-0727-1900.json`。四个正式流程图都必须纳入 Git，并保留稳定文件名。worker 的实际实现位于 `apps/workers/rabbitmq_worker.py`，根目录 `rabbitmq_worker.py` 只是兼容启动器；旧命令仍然有效。

本地调试入口位于 `apps/cli/`、`apps/builders/` 和 `apps/diagnostics/`，生产 systemd 不会启动这些一次性工具。
