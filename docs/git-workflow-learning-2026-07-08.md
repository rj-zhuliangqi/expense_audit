# Git 实战学习文档（案例版）

日期：2026-07-08  
项目：expense_audit  
分支：feature/cumulative-expense-amount-optimization

---

## 1. 这份文档学什么

这份文档基于一次真实操作过程，目标是掌握以下 Git 场景：

1. 拉取远程最新代码前，如何保护本地改动
2. 本地有改动时，为什么 pull 会被阻止
3. 什么时候用 stash，怎么恢复
4. reset --hard 的影响与风险
5. 冲突怎么处理（以单文件为例）
6. 出问题后的排查与恢复思路

---

## 2. 案例背景

当时仓库状态：

- 本地分支落后远程 3 个提交
- 本地有多处未提交修改
- 还有未跟踪文件
  - start_all_services.sh
  - deploy/systemd-units/

执行 pull 时出现提示：

- Please commit your changes or stash them before you merge.
- Aborting

这说明：Git 在保护你的本地改动，避免 merge 时被覆盖。

---

## 3. 关键结论先记住

1. 普通 pull 不会强行吞掉你未提交改动；冲突时会中止并提示。
2. reset --hard 会重置已跟踪文件到目标提交，风险很高。
3. 未跟踪文件通常不受 reset --hard 影响，但在特定场景下可能消失（例如路径冲突、工作区重建、后续清理命令等）。
4. 最稳做法：先 stash -u，再拉取，再恢复。

---

## 4. 推荐标准流程（最安全）

### 4.1 拉取前先备份本地改动

执行：

    git stash push -u -m "before pull YYYY-MM-DD"

说明：

- -u 会把未跟踪文件也一起保存
- 这是本案例能恢复 start_all_services.sh 和 deploy/systemd-units/ 的关键

### 4.2 拉取远程最新

执行：

    git fetch origin
    git pull --ff-only

说明：

- --ff-only 只接受快进更新，避免隐式 merge 提交
- 若不是快进，会明确失败，便于你决定下一步（rebase 或 merge）

### 4.3 恢复本地改动

执行：

    git stash list
    git stash pop stash@{0}

说明：

- pop 会尝试应用并删除 stash
- 如果有冲突，Git 会保留 stash 记录，方便重试

### 4.4 处理冲突并继续

查看冲突文件后处理，处理完成执行：

    git add <冲突文件>
    git status

如果全部解决，继续你的提交流程。

---

## 5. 本次真实过程回放

### 5.1 发生了什么

先执行了：

    git reset --hard origin/feature/cumulative-expense-amount-optimization

之后发现以下本地内容看不到了：

- start_all_services.sh
- deploy/systemd-units/

### 5.2 如何恢复

检查 stash，发现有备份：

    git stash list

结果包含：

- stash@{0}: On feature/cumulative-expense-amount-optimization: before use remote 2026-07-08

执行恢复：

    git stash pop stash@{0}

恢复结果：

- 大部分文件自动恢复
- node_gateway/api.py 出现冲突
- start_all_services.sh 和 deploy/systemd-units/ 恢复成功

### 5.3 冲突处理（本案例做法）

如果你明确要以 stash 版本为准，可执行：

    git restore --source=stash@{0} -- node_gateway/api.py
    git add node_gateway/api.py

再检查状态：

    git status --short

### 5.4 再确认远程是否有新提交

执行：

    git fetch origin
    git rev-list --left-right --count HEAD...origin/feature/cumulative-expense-amount-optimization

输出 0 0 代表双方一致，无需再拉。

---

## 6. 常用命令速查

### 6.1 看状态

    git status
    git status --short

### 6.2 拉取前备份

    git stash push -u -m "before pull YYYY-MM-DD"

### 6.3 查看/恢复 stash

    git stash list
    git stash show -p stash@{0}
    git stash apply stash@{0}
    git stash pop stash@{0}

### 6.4 强制对齐远程（高风险）

    git fetch origin
    git reset --hard origin/<branch>

可选清理未跟踪文件（更高风险）：

    git clean -fd

### 6.5 查最近操作记录

    git reflog -n 20

---

## 7. 什么时候用哪种策略

1. 想保留本地改动并更新远程：
   stash -u -> pull --ff-only -> stash pop
2. 本地改动不重要，全部以远程为准：
   fetch -> reset --hard -> (必要时) clean -fd
3. 不确定是否重要：
   先 stash，再做任何危险操作

---

## 8. 风险与最佳实践

1. 危险命令执行前，先做一次可回滚点
   - 最简单：stash -u
   - 更稳：临时分支 + 提交快照
2. 优先用 pull --ff-only，减少历史污染
3. 冲突文件要逐个确认，不要盲目全盘覆盖
4. 误操作后先停手，先看 reflog 与 stash list

---

## 9. 建议你固定使用的日常流程

每次准备拉远程都用下面模板：

    git status
    git stash push -u -m "before pull $(date +%F-%H%M)"
    git fetch origin
    git pull --ff-only
    git stash pop

如果 pop 有冲突：

1. 处理冲突文件
2. git add 冲突文件
3. git status 确认

---

## 10. 本案例学习总结

- 你这次能恢复成功，核心原因是提前做了 stash -u。
- reset --hard 不是不能用，但一定是“确认可丢弃本地改动”时才用。
- 对日常开发来说，stash + ff-only pull 是最稳且成本最低的习惯。
