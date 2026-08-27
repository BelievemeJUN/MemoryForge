# 文档二：从改进到现在 —— CodeMind 的完整改造历程

> 本文件逐条记录：最终目标是什么、每一步改了什么、每个文件的改动和作用、踩了什么坑。是 CodeMind 改造的"完整账本"。

---

## 1. 最终目标（一句话）

**CodeMind = 企业级代码执行 Agent**：在 echomind（会读的问答助手）底座上，长出"**会写代码 → 沙箱跑 → 确定性验证 → 自愈 → 交付**"的执行能力。

与另一个项目（deepresearch）的关系：**deepresearch 管"读"（检索/报告），CodeMind 管"做"（执行/验证）**，能力面互补、工程面大量复用。

---

## 2. 第一步：重新规划（不照单全收旧计划）

- 旧的 `PROJECT_PLAN.md` 被我中立审查，发现几个真问题：
  - "保留壳、换内核"自相矛盾（对话入口必须留，实际是**新增**执行能力，不是替换内核）；
  - 执行循环怎么和现有 Agent 集成没定义（地基不明确会推翻重来）。
- 产出权威计划 `docs/PLAN_V2.md`，**最关键决策（方案乙）**：
  - **弃用 `create_agent` 黑盒，用 LangGraph 手写整个对话+执行流程图**。为什么：黑盒里发生什么控制不了、改不动、讲不清；自己画图，每个环节可控、可讲、可改。

---

## 3. 环境改造（模型 + 数据库）

| 改动 | 细节 | 为什么 |
|---|---|---|
| **模型切到 DeepSeek** | `.env` 的 `BASE_URL=https://api.deepseek.com`、`AGENT_BASE_MODEL=deepseek-chat`（官方稳定别名） | 原 DashScope 模型名可疑 + 用户更熟 DeepSeek；两者都走 OpenAI 兼容接口，改 3 行配置代码零改动 |
| **PostgreSQL** | docker 起 `agent-postgres`（echomind/echomind123，端口 5432），6 张表自动建好 | 关系数据 |
| **Redis** | 起项目专用 `codemind-redis`（**Redis Stack**，端口 6380） | 需要 RediSearch 模块 |
| **Milvus** | **升级到 v2.5.4**（docker compose：etcd + minio + milvus 三容器） | echomind 用了 partition key isolation + 内置 BM25（2.5+ 特性，2.4 不支持） |
| **embedding 转本地** | torch CPU + sentence-transformers + **bge-large-zh-v1.5**（ModelScope 下载，1024 维，存 `data/models/`） | DashScope embedding 网络时通时不通、fastembed 因 hf 被墙不可行 → 本地化，稳定可演示 |

**踩坑记录**（环境层）：
- 环境变量污染：持久终端 `source .env` 后旧值残留，`load_dotenv()` 默认不覆盖 → 跑前 `unset` 相关变量。
- Milvus 2.4 不支持 partition key isolation + BM25 → 升级 2.5.4，并把 VARCHAR 字段的 `STL_SORT` 索引改成 `INVERTED`（2.5 里 STL_SORT 仅限数值字段）。

---

## 4. 逐里程碑改造细节（每个文件改了什么、作用）

### M1 沙箱引擎 —— `backend/sandbox/`

| 文件 | 改动/作用 | 踩坑 |
|---|---|---|
| `executor.py` | **DockerExecutor**：一次性容器跑代码，抓 stdout/stderr/退出码；**分层防御**——`--network=none` 断网、rootfs 只读（仅 `/tmp` tmpfs 限量）、`user=nobody` 非 root、`cap_drop=ALL` 去全部能力、`no-new-privileges` 禁提权、内存/CPU/PID 配额、宿主侧超时强杀、输出截断；执行前先做静态安全审查 | ① docker-py 的 `tmpfs` 参数要写 `"size=100m"`（裸 `"100m"` 报 invalid option）；② bind mount + `user=nobody` 时宿主机 0700 目录读不了 → 需 `chmod 755/644`；③ 只读文件系统要关 `__pycache__` + 无缓冲 stdout |
| `security.py` | **AST 静态审查**：解析代码查危险 import（subprocess/socket…）、危险调用（eval/exec/os.system…）、`open` 路径越界（只允许 `/work`、`/tmp` 前缀）；子串黑名单兜底。返回违规清单 → 拦截 | 后续发现 `open` 被无条件列入危险名单会误伤 `/tmp` 正常文件操作 → 移出，交路径越界检查管 |
| `limits.py` | `SandboxLimits`：内存 256m / CPU 0.5 核 / PID 64 / 磁盘 100m / 超时 30s / 输出 64KB / 代码 20KB | — |
| `scripts/run_python.sh` | 容器内入口：清空敏感环境变量（unset 密钥）、关字节码缓存、无缓冲执行 | — |
| `seccomp.json` | **（M6/P0-2 深化）** 自定义 seccomp profile：`defaultAction=ALLOW` + 显式禁掉 21 个逃逸/提权 syscall（ptrace/mount/pivot_root/bpf/setns/clone3…） | docker-py 的 `security_opt` 的 seccomp 要**内联 JSON 内容**（不是路径，daemon 把值当 JSON 解析） |

**冒烟**（`backend/test/sandbox_smoke.py`）：8/8——正常代码跑通、语法错误如实报、危险代码被拦（os.system/subprocess/越界 open/eval）、死循环超时强杀。

### M2 对话 + 执行层 —— `backend/chat/` + `backend/workflow/`

| 文件 | 改动/作用 | 踩坑 |
|---|---|---|
| `chat/llm.py` | 共享 LLM 构造（DeepSeek），抽出来**避免循环导入** | — |
| `chat/state.py` | `ChatState`：messages（`add_messages` 增量合并）、user_id、thread_id、knowledge_base_id、intent | — |
| `chat/graph.py` | **LangGraph 总图**：`START → intent（意图判断）→ 条件路由 → chat / exec / read`。`intent` 用 `with_structured_output` 让 LLM 输出 JSON（`{intent, reasoning}`），不是关键词匹配；`_route` 按意图分发；`_exec_node` 调执行子图；`_read_node` 走知识库/记忆检索 | — |
| `chat/streaming.py` | **SSE 流式**：`astream(stream_mode=["messages","updates"])`，`messages` 拿增量 token、`updates` 拿节点完整返回；**只流用户可见的 chat 节点**（用 metadata 的 `langgraph_node` 过滤掉 intent 内部 JSON），exec/read 的完整结果用 updates 补发；`collect` 参数收集最终回复（存库用） | **修复了"内部思考 JSON 泄露给前端"的 bug**（`{"intent":"kb"...}` 会逐字流出）——按消息来源节点做白名单过滤 |
| `chat/server.py` | 独立 FastAPI：`/api/sandbox/chat`（SSE）+ `/health`；lifespan 持有 checkpoint + 启动后台记忆提取任务（P0-1a）；对话后 `_persist_conversation` 写库 | — |
| `chat/checkpointer.py` | **PostgreSQL checkpoint**（M4-1 会话隔离）：`AsyncPostgresSaver`，提供 `create_checkpointer_cm()` async 上下文管理器 | Redis Stack 的 RedisAI/RedisGears 模块在本 WSL 加载失败 → 转 Postgres；且 `AsyncPostgresSaver` 的 `from_conn_string` 是 asynccontextmanager，**必须 `async with` 包住**（手动 `__aenter__` 连接会被提前关闭） |
| `workflow/state.py` | `ExecState`：task/plan/code/stdout/stderr/exit_code/timed_out/security_blocked/error + M3 自愈（tests/attempts/max_attempts/feedback/passed/final）+ M5 成本（tokens）+ P0-1b 记忆（prefs） | — |
| `workflow/exec_loop.py` | **执行子图**：`plan→write→execute→verify→fix`（自愈循环）+ 硬计数熔断；`_start_router`（已有 code 直接 execute，否则 plan→write）；token 记账；P0-1b 的 WRITE_PROMPT 注入用户偏好 | 测试传 code 会被 write 覆盖 → 加 start 路由解决 |

**冒烟**：`chat_minimal`（多轮记忆 2/2）、`chat_intent`（四类意图 4/4）、`chat_exec`（斐波那契→沙箱跑出 55）、`chat_read`（查报销→精准返回）。

### M3 验证 + 自愈 —— `backend/verifier/`

| 文件 | 改动/作用 |
|---|---|
| `verifier/assertions.py` | 输出比对三模式：`exact`（精确）/ `fuzzy`（忽略空白大小写）/ `float`（浮点容差，0.1+0.2 不误判） |
| `verifier/test_runner.py` | `run_code_with_tests`：沙箱跑代码 + hidden test 比对，分类 4 档（passed/failed/timeout/error） |

**自愈机制**（写在 `workflow/exec_loop.py`）：verify 用确定性 hidden test（不靠 LLM 自评）；失败 → 把失败原因喂给 fix（LLM 修）→ 再 execute → 再 verify；**attempts 硬计数**，超 `max_attempts` 熔断返回"修不好+原因"（不依赖 LLM 自觉——deepresearch 教训）。

**冒烟**：`verifier_smoke`（7/7）、`chat_selfheal_smoke`（错代码第 2 轮修对 + 熔断返回原因）。

### M4 会话 + 任务 —— `chat/checkpointer.py` + `backend/tasks/`

| 文件 | 改动/作用 |
|---|---|
| `chat/checkpointer.py` | 见上（M2 表）：会话隔离——每个 `thread_id` 独立存档，历史自动恢复、互不串门 |
| `tasks/models.py` | `TaskStatus`（queued/running/succeeded/failed/cancelled）+ `Task` 数据模型 |
| `tasks/manager.py` | `TaskManager`：Redis 存（Hash `task:{id}` + ZSet 索引 `tasks:all`）；**合法流转校验**（终态不可改）、可取消、列表查询；payload/result 序列化 JSON |

**冒烟**：`chat_checkpoint_smoke`（历史恢复 + 会话隔离）、`tasks_smoke`（7/7 创建/流转/非法拒绝/取消/列表）。

### M5 评测体系 —— `backend/evals/`

| 文件 | 改动/作用 |
|---|---|
| `evals/metrics.py` | **四层指标**：用例级（通过率/运行正确率/超时率）、循环级（自愈成功率/一次通过率/平均轮数）、安全级（危险命令拦截/资源滥用拦截）、成本级（token/执行次数/单题成本）；全部带 **bootstrap 置信区间**（样本少时诚实呈现不确定性，deepresearch 方法论平移） |
| `evals/run_eval.py` | 加载题目集 → 每题跑执行子图/安全沙箱 → 统计指标 → 输出 markdown 报告；支持 `pre_code`（自愈题初始错误代码）和 `expect_blocked`/`expect_resource_killed`（安全题） |
| `evals/cases/demo_cases.yaml` | 题目集：5 道生成题 + 3 道自愈题（`pre_code` 错误代码）+ 3 道安全题 |
| `evals/cases/security_cases.yaml` | **安全强化题集**：6 道静态拦截（rm -rf/subprocess/读 passwd/读 shadow/eval/__import__）+ 4 道资源滥用（fork 炸弹/内存炸弹/CPU 死循环/磁盘填满） |

**坑**：DeepSeek 的 usage 在 langchain 的 `usage_metadata`（不是 response_metadata），token 记账要读对位置。

### M6 安全强化 + P0/P1 补充

| 项 | 改动/作用 | 结果 |
|---|---|---|
| **安全测试集** | `security_cases.yaml` 真实攻击向量 | 危险命令拦截 100%（6/6）+ 资源滥用拦截 100%（4/4：fork 静态拦/OOM 137/超时/磁盘满） |
| **open 误伤修复** | `security.py`：`open` 移出无条件危险名单，交路径越界检查 | 修掉一个误报 bug（`/tmp` 正常文件操作被误拦） |
| **P1 自愈评测题** | `demo_cases.yaml` 加 `pre_code` 初始错误代码题；`run_eval` 支持 | **一次通过率 27% → 最终通过率 100%**，自愈成功率 45.45% [18%~73%] |
| **P0-1a 记忆接入** | `chat/server.py`：对话后 `_persist_conversation` 写 `raw_conversations`；lifespan 挂后台提取任务（`run_compression_task`，惰性加载，每 3 分钟） | 对话入库验证通；阈值验证（16 token < 1000 正确跳过） |
| **P0-1b 记忆注入执行** | `chat/graph.py` `_retrieve_user_prefs`（检索程序性记忆，Milvus 不可用降级空）；`_exec_node` async 传 prefs；`exec_loop` WRITE_PROMPT 加 `{prefs}` 段 | 验证检索到"偏好 Python + 中文注释"并注入 |
| **echomind 真 bug 修复** | `memory_manager.py`：`update_memory_last_access_time` 的 upsert 缺 `user_id`/`thread_id`（自建 Milvus 强制分区键字段）→ 加字段 + 调用处 try/except 容错 | 记忆检索不再崩 |

---

## 5. 全量回归与最终状态

- 全量回归 `backend/test/run_all.sh`（7 个核心冒烟）+ checkpoint + tasks = **9/9 全绿**。
- 当前 CodeMind 具备完整闭环：**会读**（本地向量化 + Milvus 检索）+ **会做**（写代码→沙箱跑）+ **会自愈**（hidden test + 硬熔断）+ **安全可量化**（静态拦截 + 资源滥用拦截 + seccomp）+ **评测有数据**（四层指标 + 置信区间）+ **记忆个性化**（对话→提取→执行注入）。
