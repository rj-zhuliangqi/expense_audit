# Codex CLI / VS Code 插件显示 `max` 模式

## 现象

当前自定义 Codex provider 的 `model/list` 已经返回 `gpt-5.6-luna` 的五档 reasoning effort：

```text
low, medium, high, xhigh, max
```

但 CLI 或 VS Code Codex 插件的选择器仍可能只有 `high` / `xhigh`。原因是两层配置同时参与筛选：

1. CLI 读取 `~/.codex/config.toml` 引用的 model catalog；
2. VS Code 插件 webview 对 `model/list` 返回值再次经过内置的 reasoning allow-list 过滤。

## 修复

使用仓库中的可重复脚本：

```bash
python3 tools/codex/enable_max_reasoning.py
```

脚本会：

- 确保 `gpt-5.6-luna` 在独立 catalog 中包含 `low/medium/high/xhigh/max`；
- 确保 `~/.codex/config.toml` 引用 `aifault-codex-model-catalog.json`，避免被旧的 cc-switch catalog 覆盖；
- 找到当前 OpenAI Codex VS Code 扩展的 webview bundle，把默认 allow-list 从
  `low/medium/high/xhigh` 扩展为 `low/medium/high/xhigh/max`；
- 为修改过的插件 bundle 创建旁边的 `.codex-max.bak` 备份；
- 重复执行是幂等的，扩展升级后可再次执行。

先只检查、不写文件：

```bash
python3 tools/codex/enable_max_reasoning.py --check
```

如果扩展不在自动探测路径，可以显式指定：

```bash
python3 tools/codex/enable_max_reasoning.py \
  --extension-dir ~/.vscode-server/extensions/openai.chatgpt-<version>-linux-x64
```

脚本默认只处理当前已验证支持完整五档的 `gpt-5.6-luna`。不要把 `max` 添加到后端没有声明支持的模型，否则 UI 虽然显示，实际请求仍可能被 provider 拒绝。

## 验证

```bash
cd ~
codex debug models
```

目标模型应显示：

```text
gpt-5.6-luna: low, medium, high, xhigh, max
```

然后在 VS Code 执行 `Developer: Reload Window`（或完全重启 VS Code），重新打开 Codex 的模型 / reasoning picker。插件 app-server 不需要修改；它已经能够返回 `max`，修复的是前端过滤层。

## 注意

- 这是对本机 `~/.codex` 和 VS Code 扩展目录的本地修复，不会把这些机器专属文件提交到 Git。
- VS Code 扩展更新后 bundle 会被替换，需要重新运行脚本。
- `model_reasoning_effort = "high"` 仍然只是默认档位；是否显示 `max` 由 catalog 和插件筛选逻辑决定。脚本不会擅自把默认档位改成 `max`。
