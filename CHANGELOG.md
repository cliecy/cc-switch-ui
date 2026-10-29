# Changelog

## 0.7.0 —— 2025-10-29

### 新增

- **TUI 终端模式**：`cc-switch-ui tui` 在终端里直接查看 / 切换供应商与账号、启停默认
  Agent（s/x），支持 `--read-only` 只读查看。零第三方依赖（仅标准库 curses），与 Web UI
  共享同一套切换语义、密钥脱敏与审计日志（`tui_switch`）。键位：`↑/↓ j/k` 移动、
  `Enter` 切换、`g/G/Home/End` 跳转、`r` 刷新、`q` 退出。
- 切换核心下沉为 `config.switch_current()`（Web 与 TUI 共用，先校验后写盘）；审计日志
  核心下沉为 `config.append_audit()`（Web 补充请求源字段，TUI 无网络字段）。
- 新增 `tests/test_tui.py`（行渲染 / 导航 / 切换持久化 / 审计回归）。

### 测试修复

- pty 假 Agent 的 shim 统一改为 `exec cat`：dash 不做尾部 exec 优化，SIGKILL 掉 sh 后
  孤儿 cat 仍占住 pty，Linux runner 上看门狗测试永远超时（macOS bash 隐式 exec 掩盖了
  该问题）；
- log-cap 测试改为轮询等待落盘后断言，消除 drain 竞争（`0 != 64` 的偶发失败）。

## 0.6.1 — 2025-10-29（安全加固版）

针对全量代码安全审计结论的集中修复。

### 安全

- **新增 Host 白名单守卫（防 DNS rebinding）**：非回环 / 非字面 IP 的域名 Host 直接 403。
  此前 Origin 校验（`Origin.netloc == Host`）在 rebinding 场景形同虚设——恶意域名解析到
  127.0.0.1 后页面源与 Host 同名，可绕过并读取状态 / 接管终端。字面 IP（含局域网）与
  `*.local` 放行，其余域名用 `CC_ALLOWED_HOSTS=myhost.example.com` 显式放行。
- **JSON body 统一 `_json_body()`**：12 处 `get_json(force=True)` 改为宽容且类型保证的解析，
  不再依赖客户端声明 `Content-Type`，杜绝 text/plain 简单请求的歧义解析路径。
- **密钥脱敏 XSS 加固**：`mask_key()` 只输出安全字符（被剔除内容返回 `•`），前端
  `account.key_masked` 渲染前补 `escapeHtml()` —— 双重过滤防存储型 XSS。
- xterm.js 已在本版本之前完成本地打包（`static/`），无 CDN 供应链面。

### 修复

- `__version__` 从包元数据读取（原硬编码 `0.4.0` 与 pyproject 漂移两个版本）。
- 根目录与 `src/` 内 `index.html` 双副本保持同步（同步测试保留）。

### 工程

- 新增 GitHub Actions CI：错误级 ruff 门禁 + unittest（3.12/3.13）+ 构建；tag 推送时自动把
  dist 产物挂到 GitHub Release（PyPI 发布见 README「发布」一节）。
- 新增 `tests/test_guard.py`(守卫/脱敏/版本同步回归测试)与 `CHANGELOG.md`。

## 0.6.0

- Agent/Codex 多客户端支持、会话管理、CLI 版本管理、审计日志与只读模式等（详见 README）。