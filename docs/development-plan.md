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
