# Universal Agent Memory — v7

The Universal Agent Memory contract closes the four gaps the article
《LangChain、AgentScope、Mem0 深度横评：谁才是 Agent 的真正记忆系统？》
calls out against Mem0:

| Mem0 痛点 | loop-memory v7 方案 |
| --- | --- |
| 部署重（四件套：向量库 / 图库 / LLM / Embedding） | 仍然单 SQLite + 零核心依赖；图谱在 `entities` + `relations` + `entity_mentions` 表里 |
| 写入延迟 | `POST /api/v1/memories` 同步落库（毫秒级），与 SDK 的 `client.remember()` 走同一条 `upsert_memory` 路径 |
| 黑盒调试 | `loop-memory export <dir>` 写 `MEMORY.md` + `pages/*.md` + `memories.jsonl` + `graph.json` — 直接 `git init` 就能版本化 |
| 缺图谱 / 缺三维评分 / 缺主动学习 | 全部补齐，详见下文 |

## 四件大事

### A. 图谱记忆 + 3D 自适应评分

Mem0 卖点和文章主推差异化之一。loop-memory v7 把已有但未串到主路径的 `entities` / `relations` / `entity_mentions` 全部接进 `recall_hybrid`：

* **`POST /api/v1/graph/edges`** — 推送高语义关系，例如
  `{src: User, dst: Hangzhou, kind: lives_in, weight: 0.9}`。
  高频谓词集在 `loop_memory/jobs/graph.py::HIGH_SIGNAL_KINDS` 里。
* **`GET /api/v1/graph/subgraph?q=…`** — 拿一段小图（节点 + 边 + 1-hop
  邻接 + 命中的 memory id）作为 prompt grounding。
* **`POST /api/v1/graph/rebuild`** — 重新提取所有 memory 的实体并写
  `entity_mentions`，让 `graph_boost` 有据可查。
* **3D 自适应评分** — `recall_hybrid(..., adaptive=True)` 在 RRF 之上
  叠加 `importance + recency + usage + graph_degree` 四维混合分，
  公式：`0.6 * RRF + 0.4 * AdaptiveScore`，再乘以
  `1 + graph_boost ∈ [0, 1.5]`。所有计算都是纯函数 `adaptive_score`，
  可单测。

### B. 认知级自动筛选 / 沉淀 / 修正

文章第 7 节论点：真正的记忆系统是 *认知过程*，不是数据库。v7 新增
`loop_memory/jobs/cognitive.py::cognitive_sleep`：

| `kind` | 触发条件 | 默认行为 |
| --- | --- | --- |
| `stale` | `age > 90d & score < 0.2 & importance < 0.3` | 建议忘记 |
| `low_value` | 从未 recall 且 `score + 0.5*importance < 0.3` | 建议忘记 |
| `merge` | Jaccard / 短文本包含度 ≥ 0.92 | 建议合并 |
| `contradict` | 复用 `jobs.contradiction.list_contradictions` | 仅标记，不自动解 |

每次扫除的结果写进 `cognitive_audit`（每条 row 一个 `suggest` /
`applied` / `reverted` 标签），所以回滚 / 审计都是 SELECT
即可。

* **`POST /api/v1/cognitive/sleep`** — `apply=true` 真删，否则只列
  建议。
* **`GET /api/v1/cognitive/audit?kind=…&action=…`** — 读历史。
* **`POST /api/v1/cognitive/audit/revert`** — 标记某条为 reverted。

### C. MEMORY.md 白盒导出 + Git 回滚

文章钦定的"白盒 + Git 可回滚"形态。`loop_memory/export/memory_md.py`
把整个 store 拍平成一个目录：

```
out_dir/
├── MEMORY.md            # 总入口（YAML front-matter + 按 tag 分组的 wiki 摘要）
├── INDEX.md             # 文件清单
├── pages/<slug>.md      # 每个 wiki 页一份 Markdown
├── memories.jsonl       # 原始记忆（每行一个 JSON，可 diff）
├── graph.json           # entities + relations
├── sessions.json        # 会话索引
└── meta.json            # schema_version + export 时间 + agent_id / user_id
```

* **`POST /api/v1/export`** — `out_dir` 是必填。
* **`POST /api/v1/import`** — 反向；`dry_run=true` 只算数量不写。
  幂等性：wiki 用 slug，memory 用 `(agent_id, user_id, external_id)`，
  缺 `external_id` 的会用 SHA1(text)[:16] 兜底。
* **`POST /api/v1/fork`**（body: `{branch_tag?: …}`）— 把当前 wiki 全量快照到
  `wiki_versions` 表，配合 `git tag` 就是一次"代码版本"。
* **`GET /api/v1/wiki/versions?page_id=…&branch_tag=…`** — 查历史。

### D. 多租户 SDK / 命名空间糖

让多 Agent / 多用户代码读起来像英语：

```python
client = MemoryClient.memory(store, agent_id="loop-memory", user_id="alice")

# 命名空间代理：每次 remember / recall 自动套上 (user, agent)
alice_ns = client.for_user("alice")
alice_ns.remember("user prefers dark mode", external_id="pref-dark")
alice_ns.recall("dark mode", limit=5)

# 不同 agent 共享同一个 store
bot_ns = client.for_agent("telegram-bot")
bot_ns.remember("...", external_id="tg-1")

# 跨空间查询 / 写
client.remember_edge("User", "Hangzhou", kind="lives_in", weight=0.9)
sg = client.subgraph("Where does User live?")
hits = client.recall_adaptive("Hangzhou", limit=8)
report = client.cognitive_sleep(apply=True)
audit = client.audit(limit=20)
ev = client.export("~/bundles/2026-07-24", agent_id="telegram-bot", user_id="alice")
```

HTTP 后端用同样的方法（`MemoryClient.http(...)`），所有调用
`urllib` 走零依赖。

## 三层 MCP 工具

`loop-memory mcp` 起的 stdio JSON-RPC 服务现在暴露 12 个工具：

| 读 | 写 / 管 |
| --- | --- |
| `recall` | `remember` |
| `list_wiki` | `forget` |
| `get_wiki` | `feedback` |
| `recent_memories` | `remember_edge` |
| `wiki_summary` | `subgraph` |
|  | `cognitive_sleep` |
|  | `audit` |

## CLI 子命令

```bash
loop-memory cognitive-sleep [--apply] [--stale-days 90] [--min-score 0.2] …
loop-memory audit [--kind stale] [--action applied] [--limit 200]
loop-memory export <out_dir> [--agent-id X] [--user-id Y] [--scope …]
loop-memory export-bundle <out_dir> [--agent-id X] [--user-id Y] [--scope …]
loop-memory import <in_dir> [--dry-run]
loop-memory fork [--branch-tag v1.0]
loop-memory graph-edge <src> <dst> [--kind lives_in] [--weight 0.9]
loop-memory subgraph <query> [--max-nodes 32]
loop-memory graph-rebuild
```

## 路由速查（`/api/v1/*`）

| Method | Path | Body | Purpose |
| --- | --- | --- | --- |
| `POST` | `/api/v1/memories` | `{text, kind?, importance?, tags?, source?, session_id?, external_id?, agent_id?, user_id?, ttl?, created_at?}` | 幂等 remember（已支持 v1） |
| `POST` | `/api/v1/memories:batch` | `{items: [...]}` | 批量 remember（已支持 v1） |
| `GET`  | `/api/v1/memories` | filters | 列表（已支持 v1） |
| `GET`  | `/api/v1/recall` | `q, adaptive=1, …` | 加 `adaptive=1` 开启 3D 评分 |
| `POST` | `/api/v1/memories/{id}/feedback` | `{value, reason?}` | 👍/👎（已支持 v1） |
| `POST` | `/api/v1/memories/feedback` | `{external_id, value, …}` | 按 external 三元组（已支持 v1） |
| `DELETE` | `/api/v1/memories` | `?external_id=…` | 按 external 删（已支持 v1） |
| `POST` | `/api/v1/graph/edges` | `{src, dst, kind?, weight?, evidence_id?}` | **新** 推语义边 |
| `GET`  | `/api/v1/graph/subgraph` | `q, max_nodes?, max_edges?` | **新** 拿小图 |
| `POST` | `/api/v1/graph/rebuild` | — | **新** 重提取实体 |
| `POST` | `/api/v1/cognitive/sleep` | `{apply?, stale_days?, min_score?, min_importance?, low_value?, merge_threshold?, limit?, record_audit?}` | **新** 跑 sweep |
| `GET`  | `/api/v1/cognitive/audit` | `kind?, action?, limit?` | **新** 读历史 |
| `POST` | `/api/v1/cognitive/audit/revert` | `{id}` | **新** 标 reverted |
| `POST` | `/api/v1/export` | `{out_dir, agent_id?, user_id?, scope?, min_importance?}` | **新** 写 MEMORY.md 目录 |
| `POST` | `/api/v1/import` | `{in_dir, agent_id?, user_id?, dry_run?}` | **新** 读回 |
| `POST` | `/api/v1/fork` | `{branch_tag?}` | **新** 快照 wiki |
| `GET`  | `/api/v1/wiki/versions` | `page_id?, branch_tag?, limit?` | **新** 查 wiki 历史 |

## Schema 增量（v6 → v7）

* 新表 `wiki_versions`：每次 `upsert_wiki_page` 都版本化；
  `fork_snapshot` 用 `branch_tag` 标记。
* 新表 `cognitive_audit`：每条 sweep 决策一条 row（kind × action）。
* 新表 `auth_tokens`：per-`(user, agent)` SHA-256 bearer；可选，
  本机默认开启。
* `memory_signals` 加了 `last_recalled_at` 索引（用于 adaptive 评分）。
* `entities` 加了 `mention_count` 索引（用于 `subgraph_for`）。
* `SCHEMA_VERSION` 6 → 7；所有迁移是 `ALTER TABLE` + `CREATE INDEX
  IF NOT EXISTS`，对老库无侵入。

## 为什么不"再做一个 Mem0"

因为我们做的是 Mem0 **没有解决**的部分：

* Mem0 用图库 + 向量库做高信号关系；loop-memory 用同一份 SQLite +
  轻量提取器。
* Mem0 把记忆封装在服务里，调试难；loop-memory 的 `MEMORY.md` /
  `git` 让任何人都能 diff / revert。
* Mem0 的写入链路是异步的；loop-memory 是同步的，所以 `upsert` → 下一
  个 `recall` 立刻能看到（`bump_signals=True` 计入 recall_count 也会
  立刻被下一轮的 adaptive 评分用上）。
* Mem0 的 SDK 是一层 client 包装；loop-memory 的 SDK 是 in-process /
  HTTP 两套后端共享同一 `MemoryClient` 接口，加上 `for_user(...)` /
  `for_agent(...)` 命名空间糖，多租户代码读起来像英语。

## 验证

* 离线：`pytest -q` 315 个测试全过（其中 v7 相关 34 个集中在
  `tests/test_universal_memory.py`）。
* 在线：`launchctl kickstart -k gui/$(id -u)/com.loopmemory.server`
  后所有 `/api/v1/*` 路由可 curl，详见 CHANGELOG。
