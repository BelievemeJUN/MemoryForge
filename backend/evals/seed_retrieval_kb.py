"""检索级评测：建测试知识库（24 篇主题文档）并生成检索题库。

流程：
1. 定义 24 篇主题文档（每篇含独特术语，query 用术语提问可精确召回）
2. 建知识库 eval_retrieval_kb（幂等：先删旧）
3. 灌入 Milvus 子块（bge-large-zh 1024 维向量 + BM25 稀疏）+ PG 父块
4. 生成 retrieval_cases.yaml（query + expected_keyword，供 run_eval 检索分支用）

用法：../.venv/bin/python evals/seed_retrieval_kb.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import yaml  # noqa: E402
from langchain_core.documents import Document  # noqa: E402

KB_ID = "eval_retrieval_kb"
USER_ID = 1

# (id, 标题, 正文[含独特术语], 检索 query, 期望命中的独特关键词)
DOCS = [
    ("doc_01", "Python 列表推导式", "Python 的列表推导式（list comprehension）用一行表达式快速生成列表，例如 [x*x for x in range(10)] 生成 0 到 9 的平方。它比 for 循环更简洁高效。", "Python 怎么用列表推导式快速生成平方列表", "列表推导式"),
    ("doc_02", "Python 装饰器", "Python 装饰器（decorator）用 @ 语法给函数附加行为，常用于日志、鉴权、性能计时。装饰器本质是接收函数返回新函数的高阶函数。", "Python 装饰器怎么实现日志功能", "装饰器"),
    ("doc_03", "冒泡排序", "冒泡排序（bubble sort）通过相邻元素两两比较交换，把最大元素逐步冒泡到末尾。时间复杂度 O(n^2)，适合小规模数据。", "冒泡排序的时间复杂度是多少", "O(n^2)"),
    ("doc_04", "快速排序", "快速排序（quick sort）选取基准值，把数组分成小于和大于两部分递归排序，平均时间复杂度 O(n log n)，是工程中最常用的排序之一。", "快速排序平均时间复杂度", "O(n log n)"),
    ("doc_05", "二分查找", "二分查找（binary search）要求数组有序，每次取中间值比较，把搜索范围减半，时间复杂度 O(log n)。", "有序数组里二分查找的时间复杂度", "O(log n)"),
    ("doc_06", "哈希表", "哈希表（hash table）通过哈希函数把键映射到桶，实现平均 O(1) 的插入与查找。Python 的 dict 就是哈希表实现。", "Python 的 dict 底层是什么数据结构", "哈希表"),
    ("doc_07", "二叉树遍历", "二叉树遍历分前序、中序、后序，前序为根左右，中序为左根右，后序为左右根；层序遍历用队列实现。", "二叉树中序遍历的顺序", "左根右"),
    ("doc_08", "动态规划", "动态规划（dynamic programming）把大问题拆成重叠子问题，用状态转移方程自底向上求解，经典例子是 0-1 背包问题。", "0-1 背包问题用什么算法", "动态规划"),
    ("doc_09", "Redis 缓存", "Redis 是内存键值数据库，支持字符串、哈希、列表、有序集合等数据结构，常用于缓存、限流、分布式锁，单线程模型保证原子性。", "什么数据库适合做缓存和分布式锁", "Redis"),
    ("doc_10", "PostgreSQL 事务", "PostgreSQL 支持 ACID 事务，通过 MVCC 多版本并发控制实现读写不阻塞，事务隔离级别可配置为读已提交、可重复读等。", "PostgreSQL 靠什么机制实现并发控制", "MVCC"),
    ("doc_11", "Milvus 向量检索", "Milvus 是开源向量数据库，支持稠密向量与稀疏向量混合检索，用 RRF 融合排序，适合 RAG 场景的语义召回。", "RAG 场景用什么数据库做向量召回", "Milvus"),
    ("doc_12", "Docker 容器", "Docker 用容器隔离进程，镜像分层复用，通过 namespace 和 cgroup 实现资源隔离与限制，一条 docker run 即可启动应用。", "用什么技术把应用打包成可隔离运行的镜像", "Docker"),
    ("doc_13", "Kubernetes 编排", "Kubernetes（K8s）是容器编排平台，管理 Pod 的调度、副本、滚动更新与服务发现，支持声明式配置。", "容器太多怎么自动调度和管理", "Kubernetes"),
    ("doc_14", "Linux 文件权限", "Linux 文件权限用 rwx 三组表示属主、属组、其他人，chmod 755 表示属主可读写执行，其他人可读执行。", "Linux 中 chmod 755 表示什么", "755"),
    ("doc_15", "HTTP 状态码", "HTTP 状态码 200 表示成功，404 表示资源不存在，500 表示服务器内部错误，429 表示请求过多被限流。", "HTTP 429 状态码表示什么", "429"),
    ("doc_16", "RESTful API", "RESTful API 用 HTTP 方法表达操作：GET 查询、POST 新建、PUT 更新、DELETE 删除，资源用 URL 标识，是无状态设计。", "RESTful 接口怎么用 HTTP 方法表达增删改查", "RESTful"),
    ("doc_17", "WebSocket", "WebSocket 建立一次 TCP 连接后双向通信，适合实时推送场景如聊天、任务进度，通过 upgrade 握手从 HTTP 升级。", "实时推送聊天消息用什么协议", "WebSocket"),
    ("doc_18", "JWT 认证", "JWT（JSON Web Token）是无状态令牌，分 header、payload、signature 三段，用 HMAC 签名防篡改，含过期时间 exp 与唯一 id jti。", "无状态令牌认证用什么标准", "JWT"),
    ("doc_19", "SQL 索引", "SQL 索引用 B+ 树加速查询，主键自动建索引，覆盖索引可避免回表，索引过多会拖慢写入性能。", "数据库用什么结构加速查询", "B+ 树"),
    ("doc_20", "Git 分支策略", "Git 分支策略常用 git flow：main 主分支、develop 开发分支、feature 功能分支，合并用 pull request 走代码评审。", "团队协作怎么用 Git 分支管理", "git flow"),
    ("doc_21", "公司报销制度", "公司报销制度：差旅报销需在出差结束后 7 个工作日内提交申请，附发票与行程单，超过 5000 元需部门总监审批。", "差旅报销要在几天内提交", "7 个工作日"),
    ("doc_22", "员工请假政策", "员工请假政策：年假需提前 3 天申请，病假需当日提交病假条，事假每次不超过 3 天，全年累计不超过 15 天。", "年假要提前几天申请", "3 天"),
    ("doc_23", "产品发布流程", "产品发布流程：开发完成 → 代码评审 → 灰度发布（10% 用户）→ 观察 24 小时 → 全量上线；异常可一键回滚到上一版本。", "新版本上线前要先经历什么阶段", "灰度"),
    ("doc_24", "客服响应规范", "客服响应规范：普通咨询 5 分钟内响应，紧急工单 30 分钟内升级给技术值班，重大故障需 10 分钟内拉起应急群并同步管理层。", "重大故障客服要在几分钟内拉应急群", "10 分钟"),
]


async def main():
    from milvus_client import get_milvus_client  # noqa: F401
    from postgresql_client import get_postgresql_client

    pg = await get_postgresql_client()
    milvus = await get_milvus_client()

    # 幂等：删旧库
    try:
        await milvus.delete_knowledge_file_chunks(KB_ID, USER_ID)
    except Exception:  # noqa: BLE001
        pass
    await pg.delete_knowledge_base(KB_ID, USER_ID)

    # 建库
    r = await pg.create_knowledge_base(KB_ID, USER_ID)
    print("建库:", r.get("message", r))

    # 灌入：每篇一个父块 + 一个子块（整篇），子块走 Milvus（自动 embedding）
    # 外键约束：父块 file_hash 必须先存在 file_metadata
    chunks, parents = [], []
    for cid, title, text, _q, _kw in DOCS:
        await pg.add_file_metadata(cid, title, KB_ID, USER_ID)
        chunks.append(
            Document(
                page_content=text,
                metadata={"parent_id": cid, "file_hash": cid, "file_name": title},
            )
        )
        parents.append(
            {
                "parent_id": cid,
                "knowledge_base_id": KB_ID,
                "text": text,
                "file_name": title,
                "file_hash": cid,
            }
        )
    await milvus.add_chunks_batch(KB_ID, chunks, USER_ID)
    n_parents = await pg.add_parent_chunk_batch(parents, USER_ID)
    print(f"灌入完成：子块 {len(chunks)}（Milvus）、父块 {n_parents}（PostgreSQL）")

    # 生成检索题库
    cases = [
        {
            "id": f"ret_{cid}",
            "retrieval": True,
            "kb_id": KB_ID,
            "query": q,
            "expected_keyword": kw,
            "top_k": 3,
            "desc": title,
        }
        for cid, title, _t, q, kw in DOCS
    ]
    out = os.path.join(os.path.dirname(__file__), "cases", "retrieval_cases.yaml")
    with open(out, "w", encoding="utf-8") as f:
        yaml.safe_dump(cases, f, allow_unicode=True, sort_keys=False)
    print(f"检索题库 {len(cases)} 题 → retrieval_cases.yaml")


if __name__ == "__main__":
    asyncio.run(main())
