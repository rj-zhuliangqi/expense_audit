# Expense Audit

费用稽核服务，负责发票数据准备、费用类型路由、流程图执行和稽核结果回写。

## 代码仓库

本项目统一使用 GitHub：

```text
https://github.com/rj-zhuliangqi/expense_audit.git
```

默认远程名为 `origin`，对应 `main` 分支。

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

正式流程图是源码的一部分，以下文件必须纳入 Git 版本跟踪：

- `graph-latest-0727-1900.json`
- `graph-latest-entertainment-0722.json`
- `graph-latest-personal-transport-0722.json`
- `graph-latest-travel-0807.json`

## 部署

服务器部署脚本和 systemd unit 说明见 [`deploy/README.md`](deploy/README.md)。生产目录应检出 `main` 的已合并提交，更新代码后再重启服务。
