# CC Switch · Web UI 管理面板

一个用来管理 **Claude Code / Codex CLI** 多供应商、多账号配置的本地 Web 面板。
后端 Flask 提供 REST API + SSE，前端是单个 `index.html`（内嵌 CSS/JS）。

## 功能

- 查看 / 切换当前供应商：Claude 官方、DeepSeek、Kimi、GLM、Qwen、OpenRouter、Claude 自定义，以及 **Codex 自定义 OpenAI**
- **自定义端点**：点供应商卡片上的 ⚙ 可改 Base URL / 模型 ID / 鉴权环境变量，接入 Anthropic 或 OpenAI Responses 兼容端点（中转、自建代理等）
- 管理 API Key：增 / 删 / 改，本地存储于 `~/.ccm_config`
- 每个供应商支持多账号，一键切换激活账号
- 启动 / 重启 / 停止 `claude` 或 `codex` 进程（通过 pty 运行）
- **CLI 安装与版本管理**：检测 Claude Code / Codex 版本，查询 npm 最新版，安装最新版或固定版本，并调用 CLI 自更新
- **保活看门狗**：勾选「保活」后，Agent 进程意外退出会自动重启（带快速失败熔断），界面显示重启次数 / 退出码
- **内嵌 xterm.js 真终端**：完整渲染 Agent CLI 的交互式 TUI（边框 / 颜色 / 方向键 / 回车），可直接在网页里打字操作
- **工作目录可选**：启动前用目录选择器（「浏览…」）或手填路径指定 Agent CLI 的运行目录
- **恢复聊天记录**：启动时可选「新会话 / 继续上次(`--continue`) / 恢复历史(`--resume`)」
- **运行状态不混淆**：分别显示「正在运行」和「下次启动」，切换连接后会明确提示是否需要重启
- **启动就绪检查**：每个连接显示缺少的 CLI、Base URL、模型或 API Key；Claude 官方登录不再被误报为缺少账号
- 深色主题，简洁现代

> 终端用 xterm.js（从 `cdn.jsdelivr.net` 加载）。离线环境会自动降级——供应商/账号管理照常可用，仅终端不可用。

## 先理解两个层次：Agent CLI 与 API 连接

面板不会把 Claude Code 和 Codex 当作同一个程序：

| 层次 | 示例 | 含义 |
|------|------|------|
| Agent CLI | Claude Code / Codex CLI | 真正在伪终端中启动的命令 |
| API 连接 | Claude 官方 / DeepSeek / Codex 自定义 OpenAI | Agent 使用的端点、模型和账号 |

Claude 官方、DeepSeek、Kimi、GLM、Qwen、OpenRouter 和 Anthropic 自定义连接会启动 `claude`；Codex 自定义 OpenAI 连接会启动 `codex`。

界面中的「正在运行」是当前进程的真实快照；「下次启动」是当前选中的配置。Agent 运行时切换连接不会偷偷修改现有进程，必须点击「切换并重启」后才会生效。

## 运行环境

- Python 3.12（用 [uv](https://github.com/astral-sh/uv) 管理依赖）
- 至少安装一个 Agent CLI：`claude`（Claude Code）或 `codex`（Codex CLI）
- `ccm` 命令为可选：存在时切换会额外调用 `ccm use <provider>`，不存在则直接写配置 + 注入环境变量

## 安装 & 启动

已发布到 [PyPI](https://pypi.org/project/cc-switch-ui/)，安装后会得到一个 `cc-switch-ui` 命令。

### 方式一：装成 CLI（推荐）

```bash
# 用 uv（推荐）或 pipx / pip 安装为全局命令
uv tool install cc-switch-ui
# 或： pipx install cc-switch-ui
# 或： pip install cc-switch-ui

# 启动（默认 127.0.0.1:8765）
cc-switch-ui

# 指定地址端口
cc-switch-ui --host 127.0.0.1 --port 8765
```

免安装、一次性试跑：

```bash
uvx cc-switch-ui
```

### 方式二：从源码运行（开发）

依赖已写进 `pyproject.toml` / `uv.lock`，`uv run` 会自动创建虚拟环境并装好依赖，无需手动 install：

```bash
git clone https://github.com/cliecy/cc-switch-ui
cd cc-switch-ui

# 首次会自动同步依赖
uv run cc-switch-ui --host 127.0.0.1 --port 8765
# 兼容入口： uv run app.py
```

启动后浏览器打开 **http://127.0.0.1:8765** 即可（`index.html` 由后端同源托管，无需单独打开文件，避免跨域问题）。

> CLI 参数：`--host`（默认 `127.0.0.1`）、`--port`（默认 `8765`）、远程监听保护开关 `--allow-remote`，以及 CLI 安装/更新开关 `--allow-cli-management`。

## Claude Code / Codex 安装与版本管理

面板默认只检测本机安装状态，不允许修改 CLI。需要安装或更新时显式启用：

```bash
cc-switch-ui --host 127.0.0.1 --port 8765 --allow-cli-management
```

然后点击右上角 **CLI 安装/版本**：

- 显示 `claude` / `codex` 的路径、当前版本和安装方式。
- 从 npm 查询最新版本。
- 输入 `latest` 或精确版本（如 `1.2.3`）进行安装、升级或降级。
- 已安装的 CLI 可调用官方自更新命令：`claude update` / `codex update`。
- 已安装的 Claude Code 可执行 `claude install latest|stable|版本号`，切换到官方原生版本或固定版本。
- Agent 运行时拒绝安装或更新，避免替换正在执行的程序。

首次安装统一使用普通用户的 npm 全局安装，不使用 `sudo`：

```bash
npm install --global @anthropic-ai/claude-code@latest
npm install --global @openai/codex@latest
```

如果现有安装不是 npm 管理的，建议使用面板中的 **CLI 自更新**，避免 npm 与原生安装产生重复可执行文件。Claude Code 还可以用 **Claude 原生版本** 按钮安装 `latest`、`stable` 或精确版本；面板不会执行 `curl | bash`。

### 中国网络

版本管理器提供两个固定下载源：

- `https://registry.npmjs.org`：npm 官方源。
- `https://registry.npmmirror.com`：第三方中国镜像，可能更快，但可能存在同步延迟和供应链风险。

中国镜像只改善 **npm 包下载**，不会改变 Claude/OpenAI 服务的地区可用性、账号资格或 API 网络连通性。请遵守服务商支持地区和当地要求；如组织有合规代理，可在服务器环境中配置标准 `HTTPS_PROXY` / `HTTP_PROXY`。不要把代理密码写入项目文件或面板日志。

安装/更新接口只允许两个固定包名、两个固定 registry 和受校验的版本号，并使用直接 argv 调用（不经过 shell）。CLI 管理功能强制只能绑定回环地址，应通过 SSH 隧道使用。

## Codex + 自定义 OpenAI 兼容端点

适用于 Ubuntu 服务器上的自建代理、中转或其它 OpenAI Responses 兼容服务：

1. 打开 **Codex · 自定义 OpenAI**，点 ⚙。
2. 填写 Base URL（通常类似 `https://proxy.example.com/v1`）和模型 ID。
3. 添加一个账号并填写 API Key，然后切换到该供应商。
4. 选择工作目录并启动。面板会用一次性的 `codex -c ...` 参数注入自定义 provider，不会改写 `~/.codex/config.toml`。

Codex 当前的自定义 provider 使用 **Responses API**。端点必须兼容 `/responses`；只有 Chat Completions `/chat/completions` 的代理不能直接使用。

服务器建议仍监听回环地址，并从本机建立 SSH 隧道：

```bash
# Ubuntu 服务器
cc-switch-ui --host 127.0.0.1 --port 8765

# 你的电脑
ssh -L 8765:127.0.0.1:8765 user@server
```

然后在本机打开 `http://127.0.0.1:8765`。如果外层已经配置 TLS 和鉴权，才使用 `--host 0.0.0.0 --allow-remote`。

### 项目结构

| 路径 | 作用 |
|------|------|
| `src/cc_switch_ui/app.py` | CLI 入口（`cc-switch-ui` 命令 → `main()`） |
| `src/cc_switch_ui/server.py` | Flask 后端：REST API + SSE |
| `src/cc_switch_ui/process.py` | Claude/Codex 进程管理（pty / 保活看门狗） |
| `src/cc_switch_ui/config.py` | 配置读写（`~/.ccm_config`） |
| `src/cc_switch_ui/index.html` | 单文件前端（内嵌 CSS/JS） |
| `app.py` | 兼容入口，指向包内 `main()` |
| `pyproject.toml` | 项目元数据 + 依赖 + `[project.scripts]` 入口 |
| `uv.lock` | 锁定的依赖版本，保证可复现 |
| `run.sh` | 守护脚本（常驻 / 崩溃自愈，无需 root） |

## 长期挂着 / 常驻部署

直接 `uv run app.py` 是**前台**运行——SSH 一断、终端一关，进程收 SIGHUP 就没了。要让它「一直挂着」，有两层保活：

- **进程级**：界面里的「保活」开关 —— Agent CLI 崩了由后端看门狗自动拉起。
- **服务级**：让 `app.py` 本身常驻、崩溃自愈 —— 用下面的 `run.sh`（app 退出时会自动清理它的 Agent 子进程，不留「失联孤儿」）。

### 方式 A：源码目录中的 `run.sh` 守护脚本（无需 root、不依赖 systemd）

用 `setsid` 脱离终端会话（SSH 断开也不死）+ 内部循环实现崩溃自动重启：

```bash
./run.sh start      # 后台启动，脱离终端
./run.sh status     # 查看状态
./run.sh log        # 跟踪日志
./run.sh stop       # 停止（连同 Agent CLI 一起清理）
./run.sh restart

# 自定义地址端口：
# 推荐通过 SSH 隧道访问；确有外层鉴权时才显式允许远程监听
CC_PORT=9000 CC_HOST=0.0.0.0 CC_ALLOW_REMOTE=1 ./run.sh start

# 显式启用 CLI 安装/更新
CC_ALLOW_CLI_MANAGEMENT=1 ./run.sh start
```

开机自启（仍然无需 root，用 crontab）：

```bash
crontab -e
# 加一行：
@reboot cd ~/cc-switch-ui && ./run.sh start
```

### 方式 B：systemd 用户服务（如果你的服务器有 systemd）

> `systemctl --user` 是**用户级**的，**不需要 root / sudo**。但前提是机器装了 systemd（部分容器 / 共享主机没有），且开机自启需要 `loginctl enable-linger`（个别受限环境可能被禁用）。用不了就走方式 A。

`~/.config/systemd/user/cc-switch.service`：

```ini
[Unit]
Description=CC Switch Web UI
After=network.target

[Service]
ExecStart=%h/.local/bin/cc-switch-ui --host 127.0.0.1 --port 8765
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now cc-switch     # 启动 + 开机自启
loginctl enable-linger "$USER"              # 登出后仍保持运行（如被禁则用方式 A）
journalctl --user -u cc-switch -f           # 看日志
```

## 配置文件

`~/.ccm_config`（JSON，权限 `0600`，由本服务读写）。结构示例：

如果配置文件不是有效 JSON，面板会先把原文件保留为 `~/.ccm_config.corrupt-时间戳`，再创建默认配置，并在页面顶部显示恢复提示；不会再静默覆盖唯一副本。

```json
{
  "current_provider": "deepseek",
  "providers": {
    "deepseek": {
      "label": "DeepSeek",
      "base_url": "https://api.deepseek.com/anthropic",
      "auth_var": "ANTHROPIC_AUTH_TOKEN",
      "model": "deepseek-chat",
      "accounts": [
        { "id": "b68b00552679", "name": "个人", "api_key": "sk-..." }
      ],
      "active_account": "b68b00552679"
    }
  }
}
```

启动 Claude Code 时，后端按当前供应商 + 激活账号注入环境变量：
`ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`（或官方的 `ANTHROPIC_API_KEY`）、`ANTHROPIC_MODEL`。
各供应商的 `base_url` / `model` 均为内置默认值，可在配置文件中按需修改。

Codex 自定义供应商使用临时的 `CC_SWITCH_CODEX_API_KEY`，并通过 CLI 配置覆盖设置 `model_provider`、`base_url`、`env_key` 和 `wire_api = "responses"`。不同客户端的鉴权和路由环境变量会在启动前清理，避免供应商之间串线。

## REST API 一览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/state` | 全量状态（密钥脱敏）+ 进程状态 |
| POST | `/api/provider/switch` | 切换当前供应商 `{provider}` |
| PUT | `/api/provider/<id>` | 编辑端点 `{label?, base_url?, model?, auth_var?}` |
| POST | `/api/account` | 新增账号 `{provider,name,api_key}` |
| PUT | `/api/account/<id>` | 编辑账号 `{provider,name,api_key?}` |
| DELETE | `/api/account/<id>?provider=` | 删除账号 |
| POST | `/api/account/activate` | 设为激活账号 `{provider,account_id}` |
| POST | `/api/agent/start` | 启动进程 `{session_mode?, cwd?, rows?, cols?}` |
| POST | `/api/agent/restart` | 重启进程（复用上次启动参数） |
| POST | `/api/agent/stop` | 停止进程 |
| POST | `/api/agent/input` | 向进程发送输入：`{raw}` 原始按键流 / `{text}` 整行 |
| POST | `/api/agent/resize` | 调整 pty 窗口大小 `{rows, cols}` |
| POST | `/api/agent/keepalive` | 开关保活看门狗 `{enabled}` |
| GET | `/api/agent/status` | 进程状态 |
| GET | `/api/agent/stream` | SSE 实时输出 |
| GET | `/api/cli/status` | 本机 CLI 路径、版本、安装方式（只读） |
| POST | `/api/cli/check` | 从固定 npm 源查询最新版本 `{registry}` |
| POST | `/api/cli/manage` | 安装/固定版本或自更新（需 `--allow-cli-management`） |
| GET | `/api/fs/list?path=` | 列出子目录（目录选择器用） |

## 安全提示

- 默认仅允许监听回环地址。建议服务器通过 SSH 隧道访问；不要把无鉴权面板直接暴露到公网。
- CLI 安装/更新默认关闭；启用后接口能修改当前用户的全局 CLI 安装，因此更不能暴露到不可信网络。
- 配置文件权限已设为 `0600`。
- 旧 `/api/claude/*` 路径继续保留为兼容别名。
