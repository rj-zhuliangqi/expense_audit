# Expense Audit

费用稽核服务，负责发票数据准备、费用类型路由、流程图执行和稽核结果回写。

## 代码仓库

本项目统一使用 GitHub：

```text
https://github.com/rj-zhuliangqi/expense_audit.git
```

默认远程名为 `origin`，对应 `main` 分支。

## 目录结构

```text
expense_audit_orchestrator/   # 核心业务库与费用类型 profile
graph_runtime/                # 流程图执行服务
node_gateway/                 # LLM/节点调用网关
apps/cli/                     # 本地 CLI（根目录入口保留兼容启动器）
apps/workers/                 # 常驻 worker
apps/builders/                # 流程图与参考数据构建器
apps/diagnostics/             # 联调和诊断工具
resources/samples/            # 样例输入
resources/reference/          # 运行所需参考数据
resources/graphs/             # 正式流程图（稳定文件名）
docs/notes/                   # 方案、问题记录
docs/operations/             # 运行与操作手册
deploy/                      # 安装脚本、启动脚本和 systemd 模板
tests/unit/                   # 单元测试
tests/graph/                 # 流程图契约测试
tests/integration/            # 集成测试说明与入口
deploy/templates/             # 可版本管理的 systemd 模板
```

应用实现已经迁移到 `apps/`，但以下根目录命令继续作为兼容入口保留：

```bash
python rabbitmq_worker.py
python execute_graph.py --help
python prepare_from_queue.py --help
```

## 本地开发

```bash
git clone https://github.com/rj-zhuliangqi/expense_audit.git
cd expense_audit
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

配置本地环境变量：

```bash
cp .env.example .env
# 按实际环境填写 OCR、审单服务、RabbitMQ 和 LLM 配置
```

运行测试：

```bash
pytest -q
```

## 分支规范

`main` 是集成、验收和生产部署基线。修复 Bug 或开发功能时，应从最新 `main` 创建独立分支，完成测试后提交，再通过审阅后的合并进入 `main`：

```bash
git fetch origin --prune
git switch main
git pull --ff-only origin main
git switch -c fix/<description>   # 或 feat/<description>
# 修改、测试并提交
git switch main
git pull --ff-only origin main
git merge --no-ff fix/<description>
git push origin main
```

正式流程图是源码的一部分，统一存放在 `resources/graphs/`，以下文件必须纳入 Git 版本跟踪：

- `resources/graphs/graph-latest-telecom-0727-1900.json`
- `resources/graphs/graph-latest-entertainment-0722.json`
- `resources/graphs/graph-latest-personal-transport-0722.json`
- `resources/graphs/graph-latest-travel-0807.json`

## 文档

- 运行与架构说明：[`docs/operations/runtime-guide.md`](docs/operations/runtime-guide.md)
- 部署说明：[`deploy/README.md`](deploy/README.md)
- 方案与问题记录：[`docs/notes/`](docs/notes/)

## 部署

服务器部署脚本和 systemd unit 说明见 [`deploy/README.md`](deploy/README.md)。生产目录应检出 `main` 的已合并提交，更新代码后再重启服务。
