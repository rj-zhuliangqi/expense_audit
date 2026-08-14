# Expense Audit 部署说明

服务器部署用的脚本与 systemd unit 模板。目标系统：Linux（已在 Ubuntu 验证 systemd 用法；CentOS 同样适用）。

## 前置条件

- **Python >= 3.12**（已在 3.14.4 验证通过；3.12/3.13 亦可）
- 服务器能联网 `pip install`（依赖走公网 PyPI；如需走内网源，设 `PIP_INDEX_URL`）
- 服务器能访问到：
  - 金蝶 OCR（`KINGDEE_OCR_BASE_URL`）
  - 真实审单网关（`AUDIT_SERVICE_URL`，默认 `https://service-uate-gw.ruijie.com.cn/`）
  - RabbitMQ（`RABBITMQ_URL`）
  - LLM API（`LLM_BASE_URL` + `LLM_API_KEY`）
- 有 sudo 权限（用于安装 systemd unit）

## 部署步骤

```bash
# 1. 拉代码
git clone https://github.com/rj-zhuliangqi/expense_audit.git
cd expense_audit
git checkout main   # 生产/集成分支，部署已合并到 main 的代码

# 2. 配置环境变量（.env 不在仓库里，需手动建）
cp .env.example .env
vim .env     # 填金蝶 OCR / RabbitMQ / LLM / 审单网关真实值

# 3. 一键装依赖 + 渲染 systemd unit
bash deploy/install.sh

# 4. 安装并启动服务（需 sudo）
sudo cp deploy/systemd-units/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now expense-graph-runtime expense-node-gateway expense-rabbitmq-worker
```

## 服务一览

| 服务 | 端口 | 说明 |
|------|------|------|
| `expense-graph-runtime` | 8090 | 图运行时 HTTP 服务（zen-engine 执行规则图） |
| `expense-node-gateway` | 8091 | 节点调用网关（图内 LLM 调用统一走这里） |
| `expense-rabbitmq-worker` | — | 常驻消费者，主链路：队列 → 数据准备 → runtime → 回写 |

启动顺序：graph-runtime 与 node-gateway 先起，worker 后起。worker 启动会校验 `AUDIT_SERVICE_URL` 不指向 `127.0.0.1`/`localhost`/`0.0.0.0`，确认 `.env` 已填真实网关。

## 常用运维命令

```bash
# 查看三个服务状态
systemctl status expense-graph-runtime expense-node-gateway expense-rabbitmq-worker

# 实时跟踪 worker 日志
journalctl -u expense-rabbitmq-worker -f

# 重启单个服务
sudo systemctl restart expense-rabbitmq-worker

# 更新代码后重启
git pull && bash deploy/install.sh && sudo systemctl restart expense-graph-runtime expense-node-gateway expense-rabbitmq-worker
```

## 关于 graph JSON

worker 的 `--graph-path` 在 unit 模板里指向仓库根的正式流程图。当前默认图为 `graph-latest-0727-1900.json`；换图时改 `deploy/expense-rabbitmq-worker.service` 里的路径，或换掉根目录的图文件后重启 worker。

## 关于进程守护

三个 unit 都设了 `Restart=on-failure` + `RestartSec=5s`：进程异常退出 5 秒后自动拉起，开机自启。

## 不需要部署的文件

`execute_graph.py`、`prepare_from_queue.py`、`main.py`、`build_telecom_list.py`、`call_audit_invoice_files.py`、`integration_test.py`、`test_*.py` 都是本地联调/调试/测试用，生产运行时不加载，保留在仓库里无害，但 systemd 不会启动它们。
