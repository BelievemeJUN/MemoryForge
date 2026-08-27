# CodeMind 项目交接文档（从 echomind 改造的代码沙箱 Agent）

> ⚠️ **本文件为 v1 历史交接记录，已由 [`PLAN_V2.md`](./PLAN_V2.md) 取代为权威计划。** v1 保留作决策溯源参考，执行请以 PLAN_V2 为准。

> 生成日期：2026-08-19
> 用途：会话切换环境后的完整交接，含所有已确认的问题、决策、设计、环境状态。新环境读完这份文档即可无缝继续。

---

## 0. 项目背景（一句话定位）

**CodeMind —— 企业级代码执行 Agent**：在 echomind（"记住你 + 懂你资料"的问答助手）底座上，长出"会写代码 → 沙箱跑 → 确定性验证 → 自愈 → 交付"的执行能力。
与 deepresearch_agent 的差异：**deepresearch 管"读"（检索/报告），CodeMind 管"做"（执行/验证）**。

- 项目路径：`/home/believemejun/agent_projects/echomind_agent/EchoMind-master`
- 改造方式：**方案 A**——保留 echomind 的壳（记忆/知识库/中间件/checkpoint 全复用），内核换成"代码沙箱执行"

---

## 1. 你问过的问题 + 结论速查

### Q1：代码沙箱 Agent 改进想法可行吗？会和 deepresearch 重合/冲突吗？
- **可行**。echomind 是纯"读型" Agent（3 个工具全是读：查知识库/查记忆/查原文），没有任何"写"能力，恰好补上执行能力。
- **能力面不冲突**（一个检索、一个执行），**工程面大量重合**（FastAPI/LangGraph/评测/安全/成本/运维都会用到），重合部分用"读型 vs 写型"叙事区分。

### Q2：编排用 deepagents 还是沿用 langchain-classic？
- **结论：不引入 deepagents，沿用 langchain-classic 中间件 + 自己用 LangGraph 搭执行循环。**
- 三个理由：
  1. deepagents 0.6.5 是老 API 时代的库，echomind 已是 LangChain 1.2.14/LangGraph 1.1.6，硬上会版本打架、等于推翻重来（注：deepresearch venv 里实际 coexists 成功，但 echomind 锁死 1.2.14，混装仍会互相破坏 pin）；
  2. **差异化**：deepresearch 用 deepagents 做检索多智能体，新项目若再套同框架 = 换皮；
  3. 改动小、复用多。

### Q3：代码沙箱方向合理吗？
- **合理**，是当下最热方向（Cursor/Claude Code/Devin/OpenHands），且正好补 deepresearch 缺的"执行"半块能力。
- 诚实难点：真沙箱安全难，但 Docker 资源级隔离 + 危险操作拦截足够面试/演示。

### Q4：需要新虚拟环境吗？怎么配？
- **必须**。原因：版本 pin 打架（echomind 锁 langchain 1.2.14 vs deepresearch 1.3.14）+ 依赖栈不同（Milvus/DashScope/Streamlit vs Chroma/MySQL/Mongo）+ 独立可复现。
- 已配好（见第 5 节）。

---

## 2. echomind 深挖（它在干什么）

### 系统全貌：5 层
| 层 | 文件 | 职责 |
|---|---|---|
| 入口 | `main.py` | FastAPI 启动 + 生命周期 + 每 3 分钟后台记忆提取任务 |
| API | `api.py` | 6 端点：上传文档/知识库增删查/删文件/流式对话 |
| 配置 | `config.py` | 全套提示词（BASE/DEFAULT/SPECIFIC/记忆提取）+ 切块参数 + 记忆评分权重 |
| Agent | `agent.py` | **单 Agent** `create_agent` + 3 工具 + 中间件 + Redis checkpoint |
| 工具 | `tools.py` | `search_knowledge_base` / `get_memory` / `get_raw_conversation_by_summary_id` |
| 向量库 | `milvus_client.py` | Milvus 连接单例，代理给两个 manager |
| 知识库检索 | `knowledeg_base_manager.py` | 集合建表（HNSW 稠密 + BM25 稀疏）、混合检索 + RRF 融合 + 降级 |
| 记忆管理 | `memory_manager.py` | 记忆集合建表、四类记忆并行检索、三维评分、冲突去重、批量入库 |
| 记忆提取 | `auto_store_memory_from_psql.py` | LLM 从对话提取记忆+画像、画像合并、入库带回滚 |
| 文档处理 | `documents_process.py` | 父子块切分（父1000/子200）、哈希去重、并行入库、Rerank |
| 去重 | `hash_storage.py` | 文件级/块级 SHA-256 去重（存 PostgreSQL） |
| 关系库 | `postgresql_client.py` | 连接池 + 6 张表（users/knowledge_bases/file_metadata/parent_chunks/chunk_hashes/raw_conversations）+ 画像缓存 |
| 缓存 | `redis_cache.py` | Redis 连接单例 |
| 数据模型 | `schemas.py` | 响应模型 + `ContextSchema(user_id, knowledge_base_id, top_k)` |

### 3 条核心数据流
1. **文档入库**：上传 → 流式写临时文件 + SHA-256 → 查重跳过 → 父子块切分 → Milvus(子块)+PostgreSQL(父块+哈希) 并行写入 → 后台任务立即返回
2. **对话问答**：SSE 流式 → Agent 判断类型 → search_knowledge_base（Milvus 混合检索 → 父块 → Rerank）或 get_memory（四类记忆并行 → 三维评分）→ 流式吐字 + 工具状态推送 → 后台存对话
3. **记忆提取**：每 3 分钟扫未摘要会话 → 超阈值 → LLM 提取（摘要/语义/情节/程序 + 画像）→ 冲突去重（相似度≥0.9）→ 小模型合并画像 → 入库带回滚

### echomind 已有亮点（复用小金库）
- 父子块混合检索 + Milvus 内置 BM25 + RRF 融合 + 双层降级
- 三层去重（文件/块/记忆冲突）
- 记忆三维评分：`0.45×相似度 + 0.25×时近衰减 + 0.3×重要性` × 类型权重
- 画像放提示词末尾（前缀缓存友好）
- 入库回滚机制、Redis 双用途（checkpoint + 画像缓存）

### echomind 的债（改造必须还）
- thread_id 直接用 user_id → **无会话隔离**（代码注释里自己承认）
- 无认证/用户体系（user_id 是 URL 参数）
- 无评测/成本/日志/告警
- 文档只支持 PDF/Word
- 前端 Streamlit（50KB app.py，很重）

---

## 3. 关键决策（已拍板，勿改）

| 决策点 | 结论 |
|---|---|
| 改造方式 | 方案 A：保留 echomind 壳，内核换沙箱执行 |
| 垂直领域 | **两者结合**：主线=代码生成+修 Bug Agent（hidden test 确定性验证+自愈）；副线=数据分析 Agent（pandas/绘图出图表）做展示 |
| 执行引擎 | **Docker 容器隔离**（一次性容器 + 资源配额；默认 `--network=none` 防数据泄露） |
| 编排框架 | 不引 deepagents；沿用 langchain-classic 中间件 + 自研 LangGraph 执行循环（plan→write→execute→verify→fix，硬预算） |
| 自愈机制 | 不放提示词软约束，用 LangGraph 图 + 硬计数强制（deepresearch 教训：软提示被 LLM 无视） |
| 主 Agent | 先沿用 echomind `create_agent`，M4 再评估是否升级 |

---

## 4. 项目设计（目录结构 + 核心模块 + 里程碑）

### 目录结构（在 echomind 基础上新增，现有文件不动）
```
backend/
├── (现有全部文件保留)
├── sandbox/          # [新] 沙箱子系统（核心）
│   ├── executor.py   #   DockerExecutor：跑容器、抓 stdout/stderr/退出码/超时
│   ├── security.py   #   危险命令/库黑名单、网络隔离、文件隔离策略
│   ├── limits.py     #   资源配额配置（内存/CPU/超时/PID/磁盘）
│   └── scripts/      #   容器内入口脚本（run_python.sh 等）
├── verifier/         # [新] 确定性验证
│   ├── test_runner.py #  跑 hidden test，分类: 通过/失败/超时/异常
│   └── assertions.py #   输出比对（exact / fuzzy / 浮点容差）
├── workflow/         # [新] 执行循环（LangGraph）
│   ├── loop.py       #   plan→write→execute→verify→fix 状态机图
│   └── state.py      #   循环状态（重试计数/预算/token 记账）
├── tasks/            # [新] 任务状态机（还 echomind 的债）
│   ├── manager.py    #   Redis 任务队列：queued→running→succeeded/failed/cancelled
│   └── models.py
├── evals/            # [新] 代码级评测体系
│   ├── cases/        #   评测题目集（含 hidden test，JSON/YAML）
│   ├── run_eval.py
│   └── metrics.py    #   四层指标统计
├── sandbox_tools.py  # [新] Agent 新工具（run_code / run_test / ...）
├── sandbox_api.py    # [新] 沙箱 API 路由（/sandbox/run、/tasks、/artifacts、/eval）
└── memory_manager.py # [改] 记忆扩展 memory_type="code_task"（记住语言偏好/常用模式）
data/
├── sandbox/          # [新] 沙箱工作区/产物目录
└── evals/            # [新] 评测报告
frontend/
├── app.py            # [改] 加沙箱面板：代码展示/运行结果/文件树/图表
└── style.css
```
另：`config.py` 加沙箱配置段；`postgresql_client.py` 加产物表/任务表（复用连接池）。

### 里程碑（每个可验收、可讲）
| 阶段 | 交付 | 验收标准 |
|---|---|---|
| **M1** 沙箱引擎 | `executor.py`+`limits.py`+`security.py` | 跑通一段 Python，拿 stdout/stderr/退出码/超时；危险命令被拦 |
| **M2** Agent 接入 | `sandbox_tools.py`+主 Agent 新工具+流式 | 对话说"写个冒泡排序"，Agent 写码并跑出结果 |
| **M3** 验证+自愈 | `verifier/`+`workflow/loop.py` | 给错代码能自愈到通过；超预算熔断生效 |
| **M4** 任务+会话 | `tasks/`+会话隔离 | 任务可查状态/可取消；两个会话互不串 |
| **M5** 评测体系 | `evals/` 四层指标 | 出第一份带 bootstrap 置信区间的评测报告 |
| **M6** 数据分析线+前端 | 副线 pandas/绘图+前端面板 | 演示"分析这份 CSV 并出图"全流程 |
| **M7** 安全+收尾 | 安全评测、上线演练、README、答辩稿 | 完整收尾 |

### 评测四层指标（差异化王牌）
- 用例级：通过率/编译率/运行正确率
- 循环级：自愈成功率/平均重试轮数/超时率
- 安全级：逃逸拦截率/危险命令拦截率/资源滥用拦截率
- 成本级：token/执行次数/单题成本
- 全部带 bootstrap 置信区间（deepresearch 方法论平移）

---

## 5. 环境配置状态（已完成）

- ✅ 独立 venv：`.venv/`（系统 Python 3.12.3 创建）
- ✅ 依赖装齐：langchain-1.2.14 / langchain-classic-1.0.3 / langgraph-1.1.6 / pymilvus-2.6.10 / streamlit-1.56.0 / dashscope-1.25.15 / fastapi-0.135.3 等
- ✅ `requirements.txt` 已从 UTF-16 转 UTF-8（备份 `requirements.utf16.bak`）
- ✅ `.env` 已从 `.env.example` 复制（**占位值，需填真实配置**）
- ✅ 预装 `docker` SDK 7.1.0（`requirements-sandbox.txt` 单独管理）
- ✅ `.vscode/settings.json` 指向新 venv
- ✅ 冒烟测试：backend 11 个模块全部导入成功

### 新环境起步命令
```bash
cd /home/believemejun/agent_projects/echomind_agent/EchoMind-master
deactivate 2>/dev/null; source .venv/bin/activate   # 激活本项目 venv（别再用 deepresearch 的）
# VS Code: 右下角选解释器 ./.venv/bin/python
```

### 待办
- [ ] 填 `.env` 真实配置（DASHSCOPE_API_KEY / Milvus_url+Token / DATABASE_URL / REDIS_URL）——做 M2 前需要
- [ ] **启用 Docker WSL 集成**（M1 前必须）：Docker Desktop → Settings → Resources → WSL Integration → 打开当前发行版；验证 `docker run hello-world`
- [ ] M1 开工
