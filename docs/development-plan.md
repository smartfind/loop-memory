# Loop Memory 开发计划

> 生成日期：2026-07-25
> 基于代码审查结果

---

## 一、安全问题（高优先级）

### 1.1 前端认证缺失 [高危]

**问题描述：**
前端暴露的所有管理端点（安装钩子、运行 LLM 任务、保存 API Key、重建图谱、删除/合并 Wiki 页面等）均未携带认证令牌或 CSRF Token。

**受影响文件：**
- [js/api.js:119-164](loop_memory/serve/static/js/api.js#L119-L164)
- [js/components/Diagnostic.js:41-67](loop_memory/serve/static/js/components/Diagnostic.js#L41-L67)
- [js/components/Wiki.js:148-216](loop_memory/serve/static/js/components/Wiki.js#L148-L216)
- [js/components/Settings.js:333-375](loop_memory/serve/static/js/components/Settings.js#L333-L375)

**风险：**
- 如果服务绑定到非本地地址，任何网站都可以触发管理操作
- 恶意网页可诱导用户浏览器发起删除/修改请求

**修复方案：**
1. 后端：为所有 `/api/admin/*`、import、delete、merge、hook 安装路由添加认证
2. 前端：在 `fetchJSON` 中添加认证 Header（如 Bearer Token）
3. 如使用 Cookie 认证：添加 CSRF 保护
4. 默认限制服务只能从 loopback 访问

---

### 1.2 v-html XSS 风险 [中危]

**问题描述：**
Dashboard 组件使用 `v-html` 渲染 Markdown 内容：
- [js/components/Dashboard.js:1312](loop_memory/serve/static/js/components/Dashboard.js#L1312)

当前实现使用自定义 Markdown 渲染器，已做防御性转义，但不是用标准安全库。

**修复方案：**
1. 首选：使用 DOMPurify 对生成的 HTML 进行清理
2. 或者：改用 Vue 文本节点渲染 Markdown
3. 添加回归测试用例：`<img src=x onerror=alert(1)>`, `javascript:` URLs 等

---

### 1.3 i18n JSON 注入 [中危]

**问题描述：**
服务端注入 i18n JSON 时仅替换 `</script>` 字符串：
- [serve/app.py:149-163](loop_memory/serve/app.py#L149-L163)

未处理大小写变体和 HTML 解析边缘情况。

**修复方案：**
1. 使用 HTML 安全序列化器转义 `<`, `>`, `&`, `/`
2. 或者：将字典作为常规 JSON 资源提供，不注入到 HTML
3. 使用框架专用序列化器

---

### 1.4 外部 CDN 依赖无完整性保护 [低危]

**问题描述：**
Vue 从 unpkg CDN 加载，ES Module 方式无法使用 `<script integrity>` 保护：
- [js/main.js:10](loop_memory/serve/static/js/main.js#L10)

**修复方案：**
1. 首选：本地打包 Vue（符合项目 local-first 设计理念）
2. 或使用受控的资产托管
3. 添加严格的 CSP 限制脚本来源

---

### 1.5 缺少安全响应头 [低危]

**问题描述：**
`index.html` 无 CSP meta 标签，无其他浏览器安全加固头。

**修复方案：**
添加响应头：
```
Content-Security-Policy
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Permissions-Policy
X-Frame-Options: DENY
```

---

### 1.6 IDOR 风险 [中危]

**问题描述：**
前端接受 API 返回的 ID 并发回后端做变更操作，后端必须验证调用者是否有权限操作该对象。

**受影响文件：**
- [js/api.js:92-109](loop_memory/serve/static/js/api.js#L92-L109)
- [js/components/Wiki.js:79-84](loop_memory/serve/static/js/components/Wiki.js#L79-L84)
- `bulkScopeWiki` 缺少 page_ids 时会影响所有页面 [js/components/Wiki.js:379-389](loop_memory/serve/static/js/components/Wiki.js#L379-L389)

**修复方案：**
后端必须：
1. 每个对象查询和变更都要做授权校验
2. 不要依赖对象 ID 的不可猜测性
3. 对 `bulkScopeWiki` 的 page_ids 做显式校验

---

## 二、性能优化

### 2.1 SQLite 索引检查

需验证 `sqlite_store.py` 中常见查询字段（memory.id, memory.scope, memory.created_at, wiki.page_id 等）是否有适当索引。

### 2.2 N+1 查询风险

检查 `retrieval.py` 和图谱相关代码是否存在 N+1 查询问题。

### 2.3 向量检索配置

如使用 Chroma backend，确认批量写入和查询优化配置。

---

## 三、代码质量

### 3.1 未使用的 import

以下文件 import 了 `escapeHtml` 但未使用：
- [js/components/Timeline.js:19](loop_memory/serve/static/js/components/Timeline.js#L19)
- [js/components/Wiki.js:11](loop_memory/serve/static/js/components/Wiki.js#L11)

### 3.2 错误消息中的敏感信息

`Settings.js` 中的错误 toast 使用 `e.message`，后端需确保错误字段不包含凭证或请求头。

---

## 四、待优化的前端交互

### 4.1 设置页面的 API Key 管理

当前 API Key 保存在组件内存中，清空后不可恢复。建议：
- 添加"显示/隐藏"切换按钮
- 添加"测试连接"按钮的加载状态
- 考虑使用 Password Manager 兼容的 `type="password"` 输入

### 4.2 Wiki 编辑器的自动保存

当前 Wiki 编辑器无自动保存，浏览器刷新会丢失内容。建议添加草稿自动保存机制。

### 4.3 图表和图谱的加载状态

KnowledgeGraph 和 Dashboard 的大型数据集加载时无骨架屏或进度指示。

---

## 五、实施优先级建议

| 优先级 | 任务 | 工作量 |
|--------|------|--------|
| P0 | 添加基础认证机制 | 高 |
| P0 | 修复 v-html XSS（添加 DOMPurify） | 低 |
| P1 | 改进 i18n JSON 注入安全性 | 低 |
| P1 | 添加安全响应头 | 低 |
| P1 | 后端 IDOR 授权校验 | 中 |
| P2 | 本地打包 Vue | 中 |
| P2 | 清理未使用的 import | 低 |
| P2 | Wiki 编辑器自动保存 | 中 |
| P3 | API Key 管理改进 | 低 |
| P3 | 加载状态骨架屏 | 中 |

---

## 六、后续步骤

等待进一步指令，确认从哪个优先级开始实施。

---

## 附：本地开发与排障

以下步骤只描述本机开发与排障流程，不属于发布流程的一部分。

### 1. 准备环境

仓库内的 Python 代码统一使用项目自带的虚拟环境，避免与系统 `python3`
冲突（macOS 自带的 `python3` 不带 `loop_memory` 依赖）。

```bash
# 仓库根目录
cd /Users/smartfind/Documents/Codex/2026-07-10/ban/work/loop_memory

# 首次创建（仓库没有 .venv 时）
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

如果只是想跑测试或运行 CLI，不写安装也可以直接用：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m loop_memory.cli.main --help
```

### 2. 启动本地服务（完整操作流程）

主服务（FastAPI + 静态前端）监听 `127.0.0.1:7767`，由 launchd 守护。
本节按“正常启动 → 改代码后重启 → 切分支/工作树后重启”三种场景给出
完整命令，并解释每一步的作用。

#### 2.1 正常启动

```bash
# 查看当前状态和 PID
launchctl print gui/$UID/com.loopmemory.server | rg 'state =|pid =|working directory'

# 手动重启（pull 新代码后必做；'-k' 会先 kill 再 start）
launchctl kickstart -k gui/$UID/com.loopmemory.server

# 实时日志
tail -f /tmp/loop_server.log
```

`launchctl print` 输出里的 `working directory` 必须始终等于主项目
根目录：

    working directory = /Users/smartfind/Documents/Codex/2026-07-10/ban/work/loop_memory

如果输出里出现 `.worktrees/...` 或别的分支目录，说明 plist 仍然指
向旧工作树——见 2.3。

直接前台启动仅在排障时使用（按 Ctrl-C 退出）：

```bash
.venv/bin/python -m loop_memory.cli.main serve --host 127.0.0.1 --port 7767
```

#### 2.2 改完代码后重启

`com.loopmemory.server` 不会热加载，每次 `git pull` 或本地编辑后都要
手动 `kickstart -k`：

```bash
git pull   # 或本地编辑
launchctl kickstart -k gui/$UID/com.loopmemory.server
sleep 2
curl -fsS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:7767/
```

#### 2.3 切到 git worktree / 切分支后

`loop_memory` 用 `git worktree` 同时维护多个分支（例如 `security-fix`）。
plist 里写死了 `WorkingDirectory`，如果上游流程在别的 worktree 里
跑过一次 `bootstrap`，plist 可能被替换为那个 worktree 的路径，导致：

- 服务还在跑，但服务进程实际工作目录是旧 worktree。
- 主项目目录里新改的 CSS / JS / HTML 不会生效。
- 更糟的情况：旧 worktree 的某次 `git rm` 之后文件缺失，浏览器拿到
  的是残缺或旧版本 JS，常见症状就是下面 2.4 的“白板”。

修复方法：把 plist 改回主项目根目录并重启。

```bash
PLIST=$HOME/Library/LaunchAgents/com.loopmemory.server.plist

# 1. 把 WorkingDirectory 改回主项目根
/usr/bin/sed -i '' \
  's|<string>/.*/loop_memory/\.worktrees/[^<]*</string>|<string>/Users/smartfind/Documents/Codex/2026-07-10/ban/work/loop_memory</string>|' \
  "$PLIST"
grep -A1 WorkingDirectory "$PLIST"

# 2. 卸载旧服务、重新装载
launchctl bootout gui/$UID/com.loopmemory.server 2>/dev/null
launchctl bootstrap gui/$UID "$PLIST"
launchctl kickstart -k gui/$UID/com.loopmemory.server

# 3. 确认
launchctl print gui/$UID/com.loopmemory.server | rg 'state =|pid =|working directory'
curl -fsS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:7767/
```

#### 2.4 浏览器白板（页面打开后是空白）

**症状**：浏览器访问 `http://127.0.0.1:7767/` 时：

- 页面标题正确（`循环记忆 · Loop Memory`），但 `<div id="app">` 是空的。
- F12 → Console 报 `Uncaught SyntaxError: Duplicate export of 'X'`、
  `X is not a function`、`Unexpected token`，或其它模块解析错误。
- 服务 `HTTP 200` 正常返回。

**抓取真实异常**：不要猜原因，先用 headless Chrome 抓控制台和
网络面板：

```bash
TMP=$(mktemp -d)
'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  --headless=new --remote-debugging-port=9225 \
  --user-data-dir="$TMP" --no-first-run --disable-gpu \
  about:blank >/tmp/loop-chrome.log 2>&1 &
PID=$!
for i in {1..40}; do curl -fsS http://127.0.0.1:9225/json/version >/dev/null && break; sleep .25; done
node -e '
  const ws = new (require("ws"))("ws://127.0.0.1:9225/devtools/page/...");
  // 监听 Runtime.exceptionThrown，把异常文本打印出来
'
kill $PID 2>/dev/null
```

最常见的三类根因（按出现频率）：

1. **服务指向了旧 worktree**：见 2.3，plist 的 `WorkingDirectory`
   还停在被 `git rm` 过的分支。修复后重启即可。
2. **本地有未保存的 JS 语法错误**：用 `node --check` 校验文件：
   ```bash
   node --check loop_memory/serve/static/js/api.js
   ```
3. **Vue 模板在 CSP 下被拒**：必须保留
   `script-src 'self' 'unsafe-eval'`，否则 `vue.esm-browser.prod.js`
   的运行时编译器会失败。改完 CSP 后用
   `tests/test_serve_app.py::test_csp_allows_self_hosted_vue_template_compiler`
   验证。

**确认恢复**：浏览器验收以下三件事，全部为真才算修好：

- `<div id="app">` 里有内容（`document.getElementById('app').innerHTML.length > 0`）。
- 顶部 4 个 `.tabs .tab` 都已渲染。
- Runtime.exceptionThrown 为空。

```bash
# 一行快速验证（用 headless Chrome 取摘要）
'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  --headless=new --dump-dom http://127.0.0.1:7767/ | rg 'tabs|app' | head -5
```

#### 2.5 端口冲突

如果 `kickstart` 之后 `state` 是 `waiting` 而没有 `pid`：

```bash
lsof -nP -iTCP:7767 -sTCP:LISTEN
```

杀掉占用的进程再 `kickstart -k` 即可。**不要**用 `sudo lsof` 杀
系统进程——本地服务只能监听 `127.0.0.1`，不必 root。


主服务（FastAPI + 静态前端）监听 `127.0.0.1:7767`，用 launchd 守护：

```bash
# 状态 / PID
launchctl print gui/$UID/com.loopmemory.server | rg 'state =|pid ='

# 手动重启（pull 新代码后必做）
launchctl kickstart -k gui/$UID/com.loopmemory.server

# 实时日志
tail -f /tmp/loop_server.log
```

直接前台启动仅在排障时使用：

```bash
.venv/bin/python -m loop_memory.cli.main serve --host 127.0.0.1 --port 7767
```

### 3. 启动数据源 watcher

每个数据源对应一个 launchd 任务。仓库默认带的是 `claude` watcher，
其它源按 `docs/auto-capture.md` 安装即可。

```bash
# Claude 会话监听（~/.claude/sessions, ~/.claude/projects）
launchctl print gui/$UID/com.loopmemory.claude | rg 'state =|pid ='
launchctl kickstart -k gui/$UID/com.loopmemory.claude

# 日志
tail -f /tmp/loop_claude.log
```

其它可用任务：

- `com.loopmemory.codex` — Codex CLI 会话。
- `com.loopmemory.openclaw` — OpenClaw / clawx 会话。
- `com.loopmemory.weekly-research` — 每周一 03:00 自动调研。

### 4. 前端验收

前端资源全部本地化（`loop_memory/serve/static/`），禁止 CDN 加载。修改
样式或组件后，按需重新加载页面（前端 JS/CSS 已经设置为
`no-cache, must-revalidate`），无需手动清理浏览器缓存。

```bash
# 拉取新代码后
launchctl kickstart -k gui/$UID/com.loopmemory.server
# 然后浏览器访问
open http://127.0.0.1:7767
```

### 5. 跑测试与质量检查

```bash
.venv/bin/python -m pytest -q                      # 320 用例
.venv/bin/python -m ruff check .                    # 静态检查
.venv/bin/python scripts/scan_secrets.py --tracked-only   # 隐私扫描
```

`scan_secrets.py` 会扫描常见的 API key、私钥与凭证赋值；包含占位符
（`xxx`、`example` 等）的命中会被自动忽略。

### 6. 提交前代理与推送

GitHub 443 在本机被拦截，必须通过 `v2rayN` 走本地代理：

```bash
# 启动 v2rayN（默认监听 127.0.0.1:10808）
open -a v2rayN

# 配置代理并推送
git config http.proxy http://127.0.0.1:10808
git config https.proxy http://127.0.0.1:10808
git push origin main
```

推送成功后清理代理设置以免影响其他工具：

```bash
git config --unset http.proxy
git config --unset https.proxy
```

关闭 v2rayN 进程，释放 `127.0.0.1:10808` 端口：

```bash
# 强制关闭 v2rayN 主进程（默认会顺带结束 xray 子进程）
lsof -ti tcp:10808 | xargs -r kill
# 等价命令：osascript -e 'tell application "v2rayN" to quit'

# 验证端口已释放、配置已清空
lsof -nP -iTCP:10808 -sTCP:LISTEN || echo "proxy stopped"
git config --get http.proxy  || echo "no git proxy"
```

### 7. 仅本地的注意事项

`docs/development-plan.md` 是 Claude 生成的安全加固与优化方案，仅供
本机开发与 code review 参考。它已被 `git rm` 提交并从历史中删除；
要保留在工作区，请用 `git restore --source=HEAD~1 --worktree` 恢复
后保持未跟踪状态，不要 `git add` 进去。
