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
  `kind='supersede'` surfaces the merge-time supersession chain
  (audit 2026-08-30, Mem0 v2.0.19 Dream pattern).
* **`POST /api/v1/cognitive/audit/revert`** — 标记某条为 reverted。
* **`GET /api/v1/cognitive/audit/supersede`** — 走 / 列出
  memory 的 supersession 链（since 0.4.6）：

  ```bash
  curl 'localhost:7767/api/v1/cognitive/audit/supersede'
  curl 'localhost:7767/api/v1/cognitive/audit/supersede?target=<id>'   # walk chain
  curl 'localhost:7767/api/v1/cognitive/audit/supersede?by=<winner>'    # filter by winner
  ```

### B.1 Observability：`stages` / `aborted` / `abort_reason`

`CognitiveReportView`（HTTP body 与 SDK 同型）现在除了
`suggested` / `applied` / `counts` 之外，还带：

```json
{
  "stages": {
    "score":         3.2,    // 毫秒
    "stale":        41.8,
    "low_value":     0.7,
    "merge":       512.6,
    "contradict":    1.1
  },
  "aborted": false,
  "abort_reason": ""           // e.g. "deadline exceeded in stage 'merge' (512.6 ms)"
}
```

* `deadline_seconds`（自 0.4.3 起）— 任意阶段开始前都会
  比较 `time.time()` 与 `t0 + deadline_seconds`；超过则把
  `aborted=True` 并把当前阶段的耗时写进 `abort_reason`，
  已经写下的建议仍然保留（不会回滚半成品）。
* `deadline_seconds=0.0` 视作「fail-fast / 永不运行」，直接
  返回一份空报告 + `aborted=True`。
* `stages` 是 **per-stage elapsed_ms** map；前端可以拿它做
  「仍在跑」的 spinner / 进度条，比单数 `elapsed_ms` 更易
  定位慢在哪一段（参见
  `loop_memory/jobs/cognitive.py::cognitive_sleep` 与
  回归用例 `tests/test_universal_memory.py::test_cognitive_sleep_*` + `tests/test_serve_handlers.py::test_returns_all_six_stages_even_when_empty`）。

LLM 蒸馏的确定性由
[`LLM_TEMPERATURE` / `LLM_SEED`](providers.md#llm_temperature--llm_seed-环境变量-确定性蒸馏)
环境变量管，与 `cognitive_sleep` 协同使用最稳。


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
| `POST` | `/api/v1/cognitive/sleep` | `{apply?, stale_days?, min_score?, min_importance?, low_value?, merge_threshold?, limit?, deadline_seconds?, record_audit?}` | **新** 跑 sweep（自 0.4.3 起支持 deadline） |
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

## MCP 工具名对照（hypemelse/memory-mcp v0.4.0）

`hypemelse/memory-mcp v0.4.0` 暴露了 22 个 MCP 工具并采用
snake_case + `memory_` 前缀的命名规范。很多 MCP-aware 客户端
（Claude Desktop、Cursor、Codex 等）已经学到这套词汇，所以从
hypemelse 迁移到 loop-memory 的用户经常会按这套词找方法。下表
给出对应关系，方便不动代码就能找到入口（本周期只做文档映射，
不做工具重命名——重命名会破坏所有现有 loop-memory MCP 消费者）：

| hypemelse 工具 | loop-memory 等价入口 | 备注 |
| --- | --- | --- |
| `memory_recall` | `loop-memory recall '<query>'` 或 `MemoryStore.recall()` / `GET /api/v1/recall` | 命中可携带 `why: [...]`（自 0.4.6） |
| `memory_remember` | `loop-memory ingest <source>` 或 `MemoryStore.upsert_memory()` / `POST /api/v1/memories` | 写入即同步 |
| `memory_forget` | `loop-memory audit` + `cognitive-sleep --apply` 或 `DELETE /api/v1/memories/{id}` | 带审计链的 GC 走前者 |
| `memory_stats` | `loop-memory memory-stats <id>`（自 0.4.7）或 `GET /api/memories/{id}/stats` | 返回 `recall_count` / `positive` / `negative` / `age_seconds` |
| `memory_search` | `loop-memory recall '<query>' --kind fact` 或 `GET /api/v1/recall?q=…` | 同 `memory_recall` |
| `memory_delete_older_than` | `loop-memory cognitive-sleep --apply --stale-days <N>` | 走 cognitive-sleep sweep |
| `memory_export_to_obsidian` | `loop-memory export-bundle <out_dir>` + Obsidian 兼容目录布局 | markdown 格式可直接喂 Obsidian |
| `memory_import_from_json` | `loop-memory import <in_dir>`（自 0.4.5 起支持 JSONL bundle） | 用同一 `import_bundle` 入口 |
| `memory_get` / `memory_get_by_external_id` | `MemoryStore.get_memory(mid)` / `GET /api/v1/memories/{id}` 或 `?external_id=…` | 后者走列表过滤 |
| `memory_update` | `MemoryStore.upsert_memory()`（同 remember，upsert 语义） | 没单独 update |
| `memory_list` | `loop-memory recall ''` 或 `GET /api/v1/memories?limit=…` | 默认排序按 `score` 倒序 |
| `memory_graph_query` | `loop-memory subgraph '<query>'` 或 `GET /api/v1/graph/subgraph?q=…` | 实体 + 关系 |
| `memory_graph_add_edge` | `loop-memory graph-edge <src> <dst>` 或 `POST /api/v1/graph/edges` | |
| `memory_graph_rebuild` | `loop-memory graph-rebuild` 或 `POST /api/v1/graph/rebuild` | |
| `memory_export_okf` (新, OKF v0.2) | `loop-memory export-okf <out_dir> [--scope S]` 或 `POST /api/export/okf`（自 0.4.9） | 写 OKF v0.2 bundle （Google OKF v0.2 spec + akitaonrails/ai-memory 2.0 + okf-memory/okf-agent-memory），跨工具可移植 |
| `memory_recall_as_of` (新, bi-temporal) | `loop-memory recall <q> --as-of <ISO|epoch>` 或 `GET /api/recall?as_of=…`（自 0.4.9） | 走 `MemoryStore.recall_as_of()`（loomcycle v1.33+），回答「那一刻我们都知道什么」 |
| `memory_fork` | `loop-memory fork [--branch-tag T]` 或 `POST /api/v1/fork` | wiki 分支 |
| `memory_snapshot` | `loop-memory snapshot <out.memory.sqlite>`（自 0.4.7）或 `POST /api/snapshot` | 单文件 SQLite 快照 |
| `memory_restore` | `loop-memory restore <in.memory.sqlite>`（自 0.4.7）或 `POST /api/snapshot/restore` | 同上 |
| `memory_audit` | `loop-memory audit` 或 `GET /api/v1/cognitive/audit` | cognitive 决策历史 |
| `memory_audit_supersede` | `loop-memory audit-supersede [--target ID]` 或 `GET /api/v1/cognitive/audit/supersede` | 自 0.4.6 |
| `memory_consolidate` | `loop-memory cognitive-sleep --apply` 或 `POST /api/v1/cognitive/sleep` | |
| `memory_rescore` | `loop-memory rescore [--half-life 30]` 或 `POST /api/admin/rescore` | |
| `memory_install_hooks` | `loop-memory install-hooks` 或 `POST /api/install-hooks` | SessionStart hook + MCP；自 0.4.8 还会 bump `agents.last_seen_at` |
| `memory_recall_outline` | `loop-memory recall-paths <q>`（自 0.4.8）或 `GET /api/recall/outline?q=…` | L0 轮廓（只返回 `id` + `abstract` + `score` + `why`），agent-loop 用 |
| `memory_init_agent` | `loop-memory init --agent <name>`（自 0.4.8）或 `POST /api/init/agent` | 把一个 agent 注册到 `agents` 表；可附带 `--install-hooks` |

工具重命名会破坏所有现有 loop-memory MCP 消费者；等 issue tracker
里出现 ≥2 个相同重命名请求再做（不要凭直觉提前动）。
