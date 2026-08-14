# Expense Audit 开发规范

本文件是本仓库的持久化开发约定，适用于后续 Bug 修复、功能开发和流程图变更。

## 流程图版本管理

以下正式流程图是源代码的一部分，**必须纳入 Git 版本跟踪，不得通过 `.gitignore` 忽略**：

- `graph-latest-0727-1900.json`
- `graph-latest-entertainment-0722.json`
- `graph-latest-personal-transport-0722.json`
- `graph-latest-travel-0807.json`

其他临时导出、编辑器快照或本地实验图可以忽略，但不能借此忽略上面列出的正式流程图。

## Bug 修复和功能开发流程

每次修复 Bug 或添加功能，都必须遵循以下步骤：

1. 开始工作前检查工作区状态，并从最新的集成分支同步代码（默认是 `master`；如项目另有指定，以项目指定分支为准）：
   ```bash
   git fetch --all --prune
   git switch master
   git pull --ff-only
   ```
2. 从最新集成分支创建独立分支。分支名使用 `fix/<描述>`、`feat/<描述>` 或 `chore/<描述>`：
   ```bash
   git switch -c fix/<描述>
   ```
3. 只修改本次任务相关的文件；不得覆盖、清理或把其他人的工作区改动混入本次提交。
4. 完成实现后执行与改动相关的测试、静态检查和配置/JSON 校验。
5. 提交前检查 staged diff 和工作区状态，确认提交内容完整且没有无关文件。
6. 完成后必须创建 Git commit，并在交付说明中报告分支名、commit、测试结果和未提交的其他改动（如有）。

如果远端不可访问或无法确认集成分支最新状态，不得假装已完成同步；应明确说明实际使用的本地基线和原因。
