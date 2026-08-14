# Expense Audit 开发规范

本文件是本仓库的持久化开发约定，适用于后续 Bug 修复、功能开发和流程图变更。

## 流程图版本管理

以下正式流程图是源代码的一部分，**必须纳入 Git 版本跟踪，不得通过 `.gitignore` 忽略**：

- `graph-latest-0727-1900.json`
- `graph-latest-entertainment-0722.json`
- `graph-latest-personal-transport-0722.json`
- `graph-latest-travel-0807.json`

其他临时导出、编辑器快照或本地实验图可以忽略，但不能借此忽略上面列出的正式流程图。

## 分支与生产基线

- `main` 是唯一的集成、验收和生产发布分支。真实服务应部署 `main` 上已经合并的提交，不能直接部署个人开发分支。
- 禁止直接在 `main` 上开发或提交；通过独立分支和 Merge Request 合并。
- 仅创建本地 `main` 不会自动改变线上服务；远端仓库、CI/CD 和服务器部署目录也必须统一切换到 `main`。
- systemd unit 只负责启动部署目录中的代码，本身不识别 Git 分支；部署更新时必须先确认该目录检出的是 `main`，再重启服务。

## Bug 修复和功能开发流程

每次修复 Bug 或添加功能，都必须遵循以下步骤：

1. 开始工作前检查工作区状态，并从最新的 `main` 同步代码：
   ```bash
   git fetch --all --prune
   git switch main
   git pull --ff-only origin main
   ```
2. 从最新 `main` 创建独立分支。分支名使用 `fix/<描述>`、`feat/<描述>` 或 `chore/<描述>`：
   ```bash
   git switch -c fix/<描述> main
   ```
3. 只修改本次任务相关的文件；不得覆盖、清理或把其他人的工作区改动混入本次提交。
4. 完成实现后执行与改动相关的测试、静态检查和配置/JSON 校验。
5. 提交前检查 staged diff 和工作区状态，确认提交内容完整且没有无关文件。
6. 完成后必须创建 Git commit，并通过 Merge Request（或经审阅的 `git merge --no-ff`）合并到 `main`：
   ```bash
   git switch main
   git pull --ff-only origin main
   git merge --no-ff fix/<描述>
   git push origin main
   ```
7. 合并到 `main` 后才视为最新结果进入集成/生产基线；部署时从 `main` 更新代码并重启相关服务。

如果远端不可访问或无法确认 `main` 的最新状态，不得假装已完成同步；应明确说明实际使用的本地基线和原因。
