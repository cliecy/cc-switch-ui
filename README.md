# CC Switch · Web UI 管理面板

一个用来管理 **Claude Code** 多供应商 / 多账号配置的本地 Web 面板。
后端 Flask 提供 REST API + SSE，前端是单个 `index.html`（内嵌 CSS/JS）。

## 功能

- 查看 / 切换当前供应商：Claude 官方、DeepSeek、Kimi、GLM、Qwen、OpenRouter，以及一个空白的 **自定义** 供应商
- **自定义端点**：点供应商卡片上的 ⚙ 可改任意供应商的 Base URL / 模型 ID / 鉴权环境变量，接入任意 Anthropic 兼容端点（中转、自建代理等）
- 管理 API Key：增 / 删 / 改，本地存储于 `~/.ccm_config`
- 每个供应商支持多账号，一键切换激活账号
- 启动 / 重启 / 停止 `claude` 进程（通过 pty 运行）
- **保活看门狗**：勾选「保活」后，claude 进程意外退出会自动重启（带快速失败熔断），界面显示重启次数 / 退出码
- **内嵌 xterm.js 真终端**：完整渲染 claude 的交互式 TUI（边框 / 颜色 / 方向键 / 回车），可直接在网页里打字操作
- **工作目录可选**：启动前用目录选择器（「浏览…」）或手填路径指定 claude 的运行目录
- **恢复聊天记录**：启动时可选「新会话 / 继续上次(`--continue`) / 恢复历史(`--resume`)」
- 深色主题，简洁现代

> 终端用 xterm.js（从 `cdn.jsdelivr.net` 加载）。离线环境会自动降级——供应商/账号管理照常可用，仅终端不可用。

## 运行环境

- Python 3.12（用 [uv](https://github.com/astral-sh/uv) 管理依赖）
- 已安装 `claude`（Claude Code CLI）
- `ccm` 命令为可选：存在时切换会额外调用 `ccm use <provider>`，不存在则直接写配置 + 注入环境变量

## 启动

依赖已写进 `pyproject.toml` / `uv.lock`，`uv run` 会自动创建虚拟环境并装好依赖，无需手动 install：

```bash
cd ~/cc-switch-ui

# 直接启动（首次会自动同步依赖，默认 127.0.0.1:8765）
uv run app.py

# 或指定地址端口：
uv run app.py --host 127.0.0.1 --port 8765
```

然后浏览器打开 **http://127.0.0.1:8765** 即可（`index.html` 由后端同源托管，无需单独打开文件，避免跨域问题）。

> 想显式同步环境：`uv sync`。也可以用 venv 解释器直接跑：`.venv/bin/python app.py`

### 项目文件

| 文件 | 作用 |
|------|------|
| `app.py` | Flask 后端 |
| `index.html` | 单文件前端 |
| `pyproject.toml` | 项目元数据 + 依赖声明（flask） |
| `uv.lock` | 锁定的依赖版本，保证可复现 |
| `.python-version` | 指定 Python 3.12 |
| `.gitignore` | 忽略 `.venv/` 等 |
| `run.sh` | 守护脚本（常驻 / 崩溃自愈，无需 root） |

## 长期挂着 / 常驻部署

直接 `uv run app.py` 是**前台**运行——SSH 一断、终端一关，进程收 SIGHUP 就没了。要让它「一直挂着」，有两层保活：

- **进程级**：界面里的「保活」开关 —— claude 崩了由后端看门狗自动拉起。
- **服务级**：让 `app.py` 本身常驻、崩溃自愈 —— 用下面的 `run.sh`（app 退出时会自动清理它的 claude 子进程，不留「失联孤儿」）。

### 方式 A：`run.sh` 守护脚本（推荐，无需 root、不依赖 systemd）

用 `setsid` 脱离终端会话（SSH 断开也不死）+ 内部循环实现崩溃自动重启：

```bash
./run.sh start      # 后台启动，脱离终端
./run.sh status     # 查看状态
./run.sh log        # 跟踪日志
./run.sh stop       # 停止（连同 claude 一起清理）
./run.sh restart

# 自定义地址端口：
CC_PORT=9000 CC_HOST=0.0.0.0 ./run.sh start
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
WorkingDirectory=%h/cc-switch-ui
ExecStart=%h/.local/bin/uv run app.py --host 127.0.0.1 --port 8765
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

切换/启动时，后端按当前供应商 + 激活账号注入环境变量给 `claude`：
`ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`（或官方的 `ANTHROPIC_API_KEY`）、`ANTHROPIC_MODEL`。
各供应商的 `base_url` / `model` 均为内置默认值，可在配置文件中按需修改。

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
| POST | `/api/claude/start` | 启动进程 `{args?, cwd?, rows?, cols?}` |
| POST | `/api/claude/restart` | 重启进程（复用上次启动参数） |
| POST | `/api/claude/stop` | 停止进程 |
| POST | `/api/claude/input` | 向进程发送输入：`{raw}` 原始按键流 / `{text}` 整行 |
| POST | `/api/claude/resize` | 调整 pty 窗口大小 `{rows, cols}` |
| POST | `/api/claude/keepalive` | 开关保活看门狗 `{enabled}` |
| GET | `/api/claude/status` | 进程状态 |
| GET | `/api/claude/stream` | SSE 实时输出 |
| GET | `/api/fs/list?path=` | 列出子目录（目录选择器用） |

## 安全提示

- 仅监听 `127.0.0.1`，请勿暴露到公网（API Key 以明文存于配置文件）。
- 配置文件权限已设为 `0600`。
