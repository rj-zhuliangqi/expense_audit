# Gorules Editor 开发环境搭建与故障排查（WSL Ubuntu 重装后）

> 适用场景：在 Windows 上重装了 WSL Ubuntu 系统，原 `/mnt/d/gorules/editor` 代码启动不起来，`./dev-restart.sh` 报 `cargo is not installed` 或前端起不来。本文记录一次完整的从零装环境 + 排障全过程，下次照着做即可。
>
> 操作日期：2026-07-02 ~ 2026-07-03。环境：WSL2 Ubuntu 26.04 (x86_64)，Windows 侧有 Clash 代理 7890。

---

## 0. TL;DR（最短恢复路径）

如果你只想快速跑起来，按顺序执行下面五步即可。详细解释见后面章节。

```bash
# 0) 确认代理变量已生效（~/.bashrc 里有 source ~/.wsl-proxy-claude.sh）
env | grep -i proxy     # 应能看到 http_proxy=http://172.x.x.x:7890

# 1) 装 Rust（官方安装器，免 sudo）
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable
source "$HOME/.cargo/env"
cargo --version        # cargo 1.96.x

# 2) 装 Node + pnpm（官方二进制 tarball，免 sudo，版本要与项目匹配）
#    先查项目用的 pnpm 版本：grep packageManager node_modules/.modules.yaml
#    本项目是 pnpm@10.30.3，所以 Node 用 v22 LTS，pnpm 锁到 10.30.3
NODE_VER=v22.23.1
curl -fL -o /tmp/node.tar.xz "https://nodejs.org/dist/$NODE_VER/node-$NODE_VER-linux-x64.tar.xz"
curl -fsSL "https://nodejs.org/dist/$NODE_VER/SHASUMS256.txt" -o /tmp/SHASUMS256.txt
cd /tmp && grep "node-$NODE_VER-linux-x64.tar.xz" SHASUMS256.txt | sha256sum -c -
mkdir -p "$HOME/.local/opt" && rm -rf "$HOME/.local/opt/node"
tar -xJf node.tar.xz -C "$HOME/.local/opt"
mv "$HOME/.local/opt/node-$NODE_VER-linux-x64" "$HOME/.local/opt/node"
echo 'export PATH="$HOME/.local/opt/node/bin:$PATH"' >> ~/.bashrc
export PATH="$HOME/.local/opt/node/bin:$PATH"
corepack enable
corepack prepare pnpm@10.30.3 --activate    # ← 版本必须和项目一致
pnpm -v          # 10.30.3

# 3) 装 C 工具链（后端 Rust crate 编译需要，需 sudo + 代理）
echo 'Acquire::http::Proxy "http://172.28.240.1:7890/";'  | sudo tee /etc/apt/apt.conf.d/95proxy
echo 'Acquire::https::Proxy "http://172.28.240.1:7890/";' | sudo tee -a /etc/apt/apt.conf.d/95proxy
sudo apt-get update && sudo apt-get install -y build-essential pkg-config libssl-dev

# 4) 装前端依赖
cd /mnt/d/gorules/editor
pnpm install

# 5) 启动
./dev-restart.sh
# 前端: http://localhost:5173/   后端: http://localhost:3000
```

> **代理网关 IP 不是一个固定值**。本文示例里是 `172.28.240.1`，它来自 `/etc/resolv.conf` 的 nameserver（WSL NAT 模式下即 Windows 宿主 IP）。每次重装/换网络后都要重新读这个值替换。`~/.wsl-proxy-claude.sh` 已自动从 resolv.conf 读取，新终端自动生效；但 apt 永久代理文件 `/etc/apt/apt.conf.d/95proxy` 里的 IP 是写死的，网关变了要手动改。

---

## 1. 背景：为什么重装 WSL 后起不来

`./dev-restart.sh` 开头做了两个前置检查：

```bash
command -v cargo >/dev/null 2>&1 || { log "cargo is not installed"; exit 1; }
command -v pnpm  >/dev/null 2>&1 || { log "pnpm is not installed";  exit 1; }
```

重装 WSL Ubuntu 后是个**干净系统**，什么都没装，所以脚本在第一行 `cargo is not installed` 就退出了——但这只是表象。真正要补的东西有 5 项，缺任一项都会在后续阶段爆不同的错。

完整的依赖链：

```
dev-restart.sh
├── cargo (Rust)        → 编译 backend/  → 还需 C 工具链(cc/gcc/make) 链接 crate
├── pnpm (Node)         → pnpm exec vite → 起前端
│   └── 版本必须与项目 node_modules 的 packageManager 字段一致
└── 网络 → 走 Windows 代理(否则 apt/pnpm/cargo 全部被公司网络劫持)
```

---

## 2. 代理配置（最关键的前置）

### 2.1 现象
公司/校园网络会**劫持明文 HTTP 直连**：直连 `http://security.ubuntu.com/...` 返回的不是 Ubuntu 仓库，而是一个 GB2312 编码的"禁止访问" HTML 页面（249 字节）。表现成 apt 报：

```
Err:1 http://security.ubuntu.com/ubuntu resolute-security InRelease
  Clearsigned file isn't valid, got 'NOSPLIT' (does the network require authentication?)
```

而走代理才会拿到真正的 PGP 签名 InRelease（137 KB）。

### 2.2 代理怎么来
WSL2 NAT 模式下，Windows 宿主的 IP = `/etc/resolv.conf` 里的 nameserver（也是默认网关）。Clash 等代理默认监听宿主的 7890。所以代理地址形如 `http://172.x.x.x:7890`。

`~/.wsl-proxy-claude.sh` 已经做了自动探测：

```bash
# ~/.wsl-proxy-claude.sh
_wsl_gw=$(awk '/^nameserver /{print $2; exit}' /etc/resolv.conf)
if [ -n "$_w_gw" ]; then
  export HTTP_PROXY="http://$_wsl_gw:7890"
  export HTTPS_PROXY="http://$_wsl_gw:7890"
  export http_proxy="$HTTP_PROXY"
  export https_proxy="$HTTPS_PROXY"
fi
unset ALL_PROXY all_proxy
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="$NO_PROXY"
```

`~/.bashrc` 里有 `source ~/.wsl-proxy-claude.sh`，所以**新开终端自动有代理变量**。curl/git/pnpm/cargo 都认这些变量。

### 2.3 apt 走代理的坑（重要）
`apt` 通过 `sudo` 运行，而 **sudo 默认会清除环境变量**——你在普通终端 `export http_proxy=...` 再 `sudo apt-get`，代理变量传不进 sudo 子进程，apt 仍直连被劫持。

两种解法，二选一：

**A. 临时：把代理变量挂在 sudo 命令前**（变量以命令行方式传入，sudo 不清除）
```bash
sudo http_proxy=http://172.28.240.1:7890 https_proxy=http://172.28.240.1:7890 \
  apt-get update && \
sudo http_proxy=http://172.28.240.1:7890 https_proxy=http://172.28.240.1:7890 \
  apt-get install -y build-essential pkg-config libssl-dev
```

**B. 永久：写 apt 代理配置文件**（推荐，配一次以后所有 `sudo apt-get` 自动走代理）
```bash
echo 'Acquire::http::Proxy "http://172.28.240.1:7890/";'  | sudo tee /etc/apt/apt.conf.d/95proxy
echo 'Acquire::https::Proxy "http://172.28.240.1:7890/";' | sudo tee -a /etc/apt/apt.conf.d/95proxy
# 之后直接 sudo apt-get update && sudo apt-get install ... 即可
```

### 2.4 验证代理通不通
```bash
# 应返回 HTTP 200 + 正常 PGP 签名内容（开头是 -----BEGIN PGP SIGNED MESSAGE-----）
curl -sS -m 15 -x http://172.28.240.1:7890 \
  http://security.ubuntu.com/ubuntu/dists/resolute-security/InRelease | head -c 200
```

---

## 3. 安装 Rust（cargo / rustc）

官方 rustup 安装器，**免 sudo**，装到 `~/.cargo`，自动改 `~/.bashrc`：

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable
source "$HOME/.cargo/env"
cargo --version    # cargo 1.96.1
rustc --version    # rustc 1.96.1
```

> 安装时会 warn `no default linker (cc) was found`——这是下一步要解决的，先不管。

---

## 4. 安装 Node.js + pnpm（免 sudo，不用 nvm）

### 4.1 为什么不用 nvm / apt
- nvm 安装脚本是 `curl ... | bash`，从 `raw.githubusercontent.com` 拉，被安全策略拦（也属于外部脚本执行，风险偏高）。
- apt 的 nodejs 版本固定、不易切，且不带 corepack。
- **推荐**：直接下 nodejs.org 官方预编译二进制 tarball（是二进制压缩包，不是脚本），解压到 `~/.local/opt/node`，再用 corepack 激活 pnpm。

### 4.2 步骤
```bash
export https_proxy=http://172.28.240.1:7890  # 走代理下载
NODE_VER=v22.23.1   # 选 v22 最新 LTS，从 https://nodejs.org/dist/index.json 查

cd /tmp
curl -fL --retry 3 -o node.tar.xz "https://nodejs.org/dist/$NODE_VER/node-$NODE_VER-linux-x64.tar.xz"
curl -fsSL "https://nodejs.org/dist/$NODE_VER/SHASUMS256.txt" -o SHASUMS256.txt
# 注意：校验文件是 SHASUMS256.txt（整个目录的所有文件哈希），不是 .sha256

mv node.tar.xz "node-$NODE_VER-linux-x64.tar.xz"
grep "node-$NODE_VER-linux-x64.tar.xz" SHASUMS256.txt | sha256sum -c -   # 要看到 OK

mkdir -p "$HOME/.local/opt"
rm -rf "$HOME/.local/opt/node"
tar -xJf "node-$NODE_VER-linux-x64.tar.xz" -C "$HOME/.local/opt"
mv "$HOME/.local/opt/node-$NODE_VER-linux-x64" "$HOME/.local/opt/node"

# 写进 bashrc（幂等：先 grep 确认没有再加）
grep -q '.local/opt/node/bin' ~/.bashrc || echo 'export PATH="$HOME/.local/opt/node/bin:$PATH"' >> ~/.bashrc
export PATH="$HOME/.local/opt/node/bin:$PATH"

node -v     # v22.23.1
corepack enable
```

### 4.3 pnpm 版本必须与项目一致（关键坑）
项目里的 `node_modules` 是用某个 pnpm 版本装的，记录在 `node_modules/.modules.yaml` 的 `packageManager` 字段。如果用**不同大版本的 pnpm**，它会想清理/重建 node_modules，在 `dev-restart.sh` 这种**无 TTY 的非交互**场景下直接报错退出：

```
[ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY] Aborted removal of modules directory due to no TTY
```

先查项目用的版本：
```bash
grep packageManager /mnt/d/gorules/editor/node_modules/.modules.yaml
# → "packageManager": "pnpm@10.30.3"
```

然后把 corepack 的 pnpm **精确锁到这个版本**（不是 latest）：
```bash
corepack prepare pnpm@10.30.3 --activate
pnpm -v   # 10.30.3
```

> 如果 node_modules 已经是别的 pnpm 版本装的、或残缺，最省事是直接 `rm -rf node_modules && pnpm install` 重装（用正确版本的 pnpm）。

---

## 5. 安装 C 工具链（build-essential）

### 5.1 为什么需要
Rust 大量 crate 依赖系统 C 编译器/链接器，尤其这几个：`rquickjs-sys`、`zstd-sys`、`ring`、`brotli`、`psm`。没有 `cc` 时 cargo 编译会在很早就崩：

```
error: linker `cc` not found
  = note: No such file or directory (os error 2)
error: could not compile `quote` (build script) due to 1 previous error
error: could not compile `proc-macro2` (build script) ...
error: could not compile `libc` (build script) ...
```

### 5.2 安装（需 sudo + 代理，见 §2.3）
```bash
sudo apt-get update && sudo apt-get install -y build-essential pkg-config libssl-dev
```

### 5.3 验证
```bash
command -v cc gcc make ld pkg-config
cc --version          # cc (Ubuntu 15.2.0...) 15.2.0
pkg-config --version  # 2.5.1
pkg-config --modversion openssl   # 3.5.5  (libssl-dev 提供)
```

### 5.4 sudo 免密（可选，方便后续自动化）
本次还开了免密 sudo（`!` 前缀非交互 shell 里 sudo 没法弹密码框，所以要在真终端做）：
```bash
echo 'ruijie ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/ruijie-nopasswd
sudo chmod 440 /etc/sudoers.d/ruijie-nopasswd
```
> 安全提示：免密 sudo = 任何能进此 WSL 的进程都能 root，仅适合个人开发机。不用可 `sudo rm /etc/sudoers.d/ruijie-nopasswd` 撤销。

---

## 6. 编译后端 + 启动

```bash
cd /mnt/d/gorules/editor
source "$HOME/.cargo/env"
cargo build --manifest-path backend/Cargo.toml   # 首次约 2~3 分钟
```
成功标志：`Finished dev profile [unoptimized + debuginfo] target(s) in Xm Ys`，产出 `target/debug/editor`（约 186 MB debug 二进制）。

然后装前端依赖并启动：
```bash
export PATH="$HOME/.local/opt/node/bin:$PATH"
pnpm install          # 拉约 800 个包
./dev-restart.sh
```

`dev-restart.sh` 会：编译后端 → 清旧进程 → 起后端(:3000) → 起前端(vite :5173) → 写 pid 文件到 `.run/`。

成功输出：
```
[editor-dev] backend ready: http://127.0.0.1:3000
[editor-dev] frontend ready: http://127.0.0.1:5173
[editor-dev] logs: /mnt/d/gorules/editor/.run/logs
```

访问：**前端 http://localhost:5173/** ，后端 API `http://localhost:3000`。

---

## 7. 故障排查清单（按报错对号入座）

| 报错 / 现象 | 原因 | 解决 |
|---|---|---|
| `[editor-dev] cargo is not installed` | PATH 里没有 `~/.cargo/bin` | `source ~/.cargo/env`，确认 `~/.bashrc` 有 `. "$HOME/.cargo/env"` |
| `[editor-dev] pnpm is not installed` | PATH 里没有 node bin | 确认 `~/.bashrc` 有 `export PATH="$HOME/.local/opt/node/bin:$PATH"`，新开终端 |
| `error: linker 'cc' not found` | 没装 C 工具链 | §5 `sudo apt-get install -y build-essential pkg-config libssl-dev` |
| `Clearsigned file isn't valid, got 'NOSPLIT'` (apt) | HTTP 直连被网络劫持，没走代理 | §2.3 给 apt 配代理（永久写 95proxy 或临时挂 sudo 前） |
| `sudo: interactive authentication is required` 在 `!` 命令里 | 非交互 shell 没法弹密码 | 改去真 WSL 终端跑 sudo；或开免密 sudo（§5.4） |
| `ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY` | pnpm 版本与项目 node_modules 不一致，想在无 TTY 时清理 | §4.3 `corepack prepare pnpm@<项目版本> --activate`，然后 `rm -rf node_modules && pnpm install` |
| `corepack prepare pnpm@latest` 后版本不对 | latest ≠ 项目版本 | 改用具体版本号 `pnpm@10.30.3` |
| 前端 vite 起来但 `curl 127.0.0.1:5173` 超时，`curl localhost:5173` 却 200 | WSL 内 curl 偶发慢响应（非服务问题） | 用浏览器访问 http://localhost:5173/ 验证；服务实际正常 |
| `frontend failed to start` + 日志是 pnpm install 报错 | node_modules 残缺/版本不符 | `rm -rf node_modules && pnpm install`（pnpm 版本先对齐） |

---

## 8. 环境变量与路径速查

```bash
# ~/.bashrc 里应有这几行（本次都已加好）：
export PATH="$HOME/.local/bin:$PATH"
source ~/.wsl-proxy-claude.sh          # 自动注入代理变量
. "$HOME/.cargo/env"                    # Rust
export PATH="$HOME/.local/opt/node/bin:$PATH"   # Node + pnpm
```

关键路径：
- Rust：`~/.cargo/bin/{cargo,rustc}`，`~/.rustup/toolchains/stable-x86_64-unknown-linux-gnu/`
- Node：`~/.local/opt/node/bin/{node,npm,corepack,pnpm}`
- 项目运行时：`/mnt/d/gorules/editor/.run/{backend.pid,frontend.pid,frontend.port,logs/}`

---

## 9. 注意事项 / 经验

1. **代理网关 IP 会变**。重装 WSL 或换网络后，`/etc/resolv.conf` 的 nameserver 会变，`~/.wsl-proxy-claude.sh` 自动跟随；但 `/etc/apt/apt.conf.d/95proxy` 里的 IP 是写死的，网关变了要手动改（或干脆删掉 95proxy 改用临时挂 sudo 方式）。
2. **pnpm 版本对齐**是前端起不来的高频原因。先用 `grep packageManager node_modules/.modules.yaml` 查项目用的版本，corepack 锁同版本。`package.json` 里本项目没有 `packageManager` 字段，靠 node_modules/.modules.yaml 记录。
3. **`!` 前缀命令里的 sudo 没法输密码**（非交互）。需要密码的 sudo 操作去真 WSL 终端做，或先开免密。
4. **WSL Ubuntu 26.04 的 apt 源**用 deb822 格式（`/etc/apt/sources.list.d/ubuntu.sources`），codename 是 `resolute`，没有传统的 `sources.list`。改源要改 `.sources` 文件。
5. **target/debug/editor 旧二进制**（6/4 的）不能直接复用——重装后 Rust 工具链/链接器环境变了，必须重新 `cargo build` 覆盖。
6. **node_modules 旧的**（6/3 装的）也不可靠，版本可能不符，最干净是 `rm -rf node_modules && pnpm install`。
7. **dev-restart.sh 的 wait_for_port 用 ss/lsof 检端口**，WSL 里 `ss` 在 `iproute2` 包，默认有；如果哪天报 "missing ss or lsof"，`sudo apt-get install -y iproute2 lsof`。
