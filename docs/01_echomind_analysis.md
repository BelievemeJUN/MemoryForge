# 文档一：改进前的 echomind —— 它到底做了什么、亮点在哪

> 本文件客观复盘**改造之前**的 echomind 原始项目：它解决的问题、系统结构、每一条亮点细节，以及它欠下的债。作为理解 CodeMind 改造起点的基础材料。

---

## 1. 一句话定位

**echomind = 一个"个性化智能问答助手"**：能记住用户长期偏好、能查用户上传的私有资料（PDF/Word）、能基于这些做个性化回答。核心价值一句话——"让 AI 真正记住你，而非每次都是初次见面"。

它本质上是一个 **RAG（检索增强生成）+ 长期记忆** 的问答系统：回答问题时先检索相关资料和记忆，再让大模型组织答案。

---

## 2. 系统全貌：5 层架构（每个文件干什么）

echomind 是 FastAPI + LangChain 的服务，`backend/` 下有 13 个文件，分 5 层：

### 第 1 层：入口与 API

| 文件 | 作用 | 细节 |
|---|---|---|
| `main.py` | FastAPI 启动入口 | 生命周期（lifespan）里依次初始化 PostgreSQL、Milvus、Redis 三个连接；**启动一个每 3 分钟跑一次的后台任务**（记忆提取）；挂载 `api.py` 的路由 |
| `api.py` | 全部 HTTP 端点 | 共 6 个：① 上传文档 ② 创建知识库 ③ 查询知识库列表 ④ 删除知识库 ⑤ 删除文件 ⑥ 流式对话（SSE）。**注意：`user_id` 是 URL/表单参数直接传进来，没有任何认证** |
| `schemas.py` | 数据模型 | 定义了强类型上下文 `ContextSchema(user_id, knowledge_base_id, top_k)`，注入给 Agent 工具使用 |

### 第 2 层：配置

| 文件 | 作用 | 细节 |
|---|---|---|
| `config.py` | 全套配置 | 系统提示词（默认/指定知识库两套）、记忆提取提示词、文档切块参数（父块 1000 字/子块 200 字）、记忆评分权重（相似度 0.45 / 时近 0.25 / 重要性 0.3）等 |

### 第 3 层：Agent 与工具

| 文件 | 作用 | 细节 |
|---|---|---|
| `agent.py` | Agent 核心 | 用 LangChain 的 `create_agent` 建单 Agent，挂 3 个读工具 + 中间件 + Redis checkpoint（带 TTL 60 分钟、读时刷新）；中间件有：记忆检索限 1 次、知识库检索限 1 次、`dynamic_prompt`（根据知识库 id 动态切换系统提示词并注入用户画像） |
| `tools.py` | 工具集 | 3 个**只读**工具：① `search_knowledge_base`（混合检索知识库）② `get_memory`（检索四类长期记忆）③ `get_raw_conversation_by_summary_id`（按摘要 id 拿原始对话）。**这 3 个工具全是"读"，没有任何"执行/写"能力**——这是它和 CodeMind 最本质的差别 |

### 第 4 层：业务逻辑（数据管道）

| 文件 | 作用 | 细节 |
|---|---|---|
| `documents_process.py` | 文档处理 | PDF/Word 解析 → **父子块切分**（父块 1000 字、子块 200 字）→ 向量化 → 并行入库 → 可选 Rerank 重排 |
| `hash_storage.py` | 去重 | SHA-256 **文件级 + 块级**两层去重（存 PostgreSQL） |
| `knowledeg_base_manager.py` | 知识库检索 | 集合建表（`user_id` 作分区键 + 内置 BM25 函数 + HNSW 稠密索引 + 标量 INVERTED 索引）；**混合检索**（稠密向量 + BM25 稀疏）→ RRF 融合 → 双层降级 |
| `memory_manager.py` | 记忆管理 | 记忆集合（四类记忆）；**四类并行检索** + **三维评分** + 冲突去重（相似度 ≥ 0.9 标记重复）+ 批量入库 + 访问时间更新 |
| `auto_store_memory_from_psql.py` | 记忆提取 | 后台任务：扫未摘要对话 → LLM 提取（摘要/语义/情节/程序 + 用户画像）→ 画像合并 → 入库（带事务回滚）→ 标记已摘要 |

### 第 5 层：数据存储

| 文件 | 作用 | 细节 |
|---|---|---|
| `milvus_client.py` | Milvus（向量库） | 单例包装 + 用 DashScope embedding（`text-embedding-v4`，1024 维）向量化；代理给两个 manager |
| `postgresql_client.py` | PostgreSQL（关系库） | 连接池 + 6 张表（`users`/`knowledge_bases`/`file_metadata`/`parent_chunks`/`chunk_hashes`/`raw_conversations`）+ 用户画像缓存 + `add_conversation_message` 存对话 |
| `redis_cache.py` | Redis（缓存） | 连接单例；**双用途**：Agent 的 checkpoint（会话状态）+ 用户画像缓存 |

---

## 3. 三条核心数据流（项目是怎么运转的）

1. **文档入库**：用户上传 → 流式写临时文件 + SHA-256 → 查重（重复跳过）→ 父子块切分 → Milvus（子块向量）+ PostgreSQL（父块原文 + 哈希）并行写入 → 后台任务处理、接口立即返回。
2. **对话问答**：SSE 流式 → Agent 判断 → 调 `search_knowledge_base`（混合检索 → 取父块 → Rerank）或 `get_memory`（四类记忆并行 → 三维评分）→ 流式吐字 + 工具状态推送 → 后台存对话。
3. **记忆提取**：每 3 分钟扫未摘要会话 → 超 token 阈值 → LLM 提取（四类记忆 + 画像）→ 冲突去重 → 小模型合并画像 → 入库带回滚。

---

## 4. 亮点细节逐条拆解（每一条都值得讲）

### 亮点 1：父子块混合检索（检索精度 + 上下文完整性的平衡）
- **做法**：文档切成两层——**父块（1000 字，存 PostgreSQL）**、**子块（200 字，存 Milvus）**。检索时先按子块找语义相关的，再用子块 id 反查**父块原文**给模型。
- **为什么**：子块小、检索精准；父块大、上下文完整。两者结合 = 既找得准、又不丢上下文。这是 RAG 领域的经典优化。
- **对应文件**：`documents_process.py`（切分）+ `knowledeg_base_manager.py` + `postgresql_client.py`（父块存储）。

### 亮点 2：Milvus 内置 BM25 + 稠密向量混合检索 + RRF 融合 + 双层降级
- **做法**：同一个集合里既有**稠密向量**（HNSW，语义）又有**稀疏 BM25**（关键词），检索时两个都查，用 **RRF（Reciprocal Rank Fusion）** 融合排序。
- **降级策略**：混合检索失败 → 降级纯稠密检索；Rerank 失败 → 降级用 RRF 结果。**保证服务高可用**。
- **对应文件**：`knowledeg_base_manager.py`（集合 schema 里用 `FunctionType.BM25` 内置函数 + HNSW + INVERTED 索引）。

### 亮点 3：三层去重（文件/块/记忆冲突）
- **做法**：文件级 SHA-256 去重（重复文件不重复入库）、块级去重、记忆冲突去重（相似度 ≥ 0.9 标记重复过滤）。
- **为什么**：避免知识库和记忆库无限膨胀、重复内容污染检索。
- **对应文件**：`hash_storage.py` + `memory_manager.py`。

### 亮点 4：四类记忆 + 三维评分（记忆系统是它的招牌）
- **做法**：记忆分四类——**摘要**（summary）、**语义**（semantic 事实）、**情节**（episodic 历史事件）、**程序**（procedural 步骤/习惯），外加**用户画像**。
- **检索**：四类**并行**查询（耗时降低约 60%），再用三维评分融合：
  $$\text{score} = 0.45 \times \text{相似度} + 0.25 \times \text{时近衰减} + 0.3 \times \text{重要性，再乘类型权重}$$
- **为什么**：把"记忆"拆成多种维度，回答时按需取用（查事实走语义、回忆历史走情节、问操作步骤走程序）。
- **对应文件**：`memory_manager.py`。

### 亮点 5：用户画像融合（持续演进不丢失）
- **做法**：后台提取出用户画像后，用**小模型把新旧画像智能合并**（而不是覆盖），保证偏好持续累积。
- **对应文件**：`auto_store_memory_from_psql.py`。

### 亮点 6：记忆提取的阈值保护 + 事务回滚
- **做法**：只有对话 **token 超过阈值（1000）** 才触发提取，避免碎片化对话刷爆记忆库；入库带事务，失败回滚并标记，避免重复处理。
- **对应文件**：`auto_store_memory_from_psql.py`。

### 亮点 7：Redis 双用途（checkpoint + 画像缓存）
- **做法**：同一个 Redis 同时做 Agent 会话 checkpoint（TTL 60 分钟、读时刷新，短期记忆）和用户画像缓存（长期）。
- **对应文件**：`redis_cache.py` + `agent.py`。

### 亮点 8：动态提示词 + 工具限流中间件
- **做法**：`dynamic_prompt` 根据知识库 id 切换系统提示词、注入画像；`ToolCallLimitMiddleware` 限制单轮对话里记忆/知识库检索各最多 1 次（防止 Agent 反复无意义检索烧钱）。
- **对应文件**：`agent.py` + `config.py`。

---

## 5. echomind 欠下的债（改造时必须要还的）

1. **无认证/用户体系**：`user_id` 直接是 URL 参数，任何人可冒充任何用户。
2. **无会话隔离**：`thread_id` 直接用 `user_id`（代码注释里自己承认），多个会话共用一套状态。
3. **无评测/成本/日志/告警**：没有任何指标统计、token 成本追踪、结构化日志。
4. **文档格式受限**：只支持 PDF/Word。
5. **前端很重**：Streamlit 的 `app.py` 约 50KB。
6. **embedding 依赖外部 API**：用 DashScope 的 embedding（这在后来的环境里成了不稳定因素）。
