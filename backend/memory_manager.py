import os
import asyncio
import time
from dotenv import load_dotenv
from typing import List, Optional, Dict, Any
from langchain_community.embeddings import DashScopeEmbeddings
from pymilvus import AsyncMilvusClient, DataType, Function, FunctionType
from pymilvus import AnnSearchRequest, RRFRanker
import logging

logger = logging.getLogger(__name__)

# 加载环境变量
load_dotenv()

# P2 设计优化：记忆访问时间更新做 Redis 节流。
# 问题：Milvus upsert 每次检索都要全字段（含 1024 维向量）写回，高频检索写放大严重。
# 方案：Redis 记录每条记忆的最近触摸时间，同一记忆在 _TOUCH_TTL 秒内只 upsert 一次。
_TOUCH_TTL = int(os.getenv("MEM_TOUCH_THROTTLE", "60"))

_redis_client = None


def _get_redis():
    """模块级 Redis 连接缓存（server 单事件循环下安全；异常时重建）。"""
    global _redis_client
    if _redis_client is None:
        import redis.asyncio as aioredis

        _redis_client = aioredis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6380/0")
        )
    return _redis_client


class MemoryManager:
    """记忆管理器 - 负责记忆的初始化、存储、更新、检索"""

    def __init__(
        self,
        client: AsyncMilvusClient,
        embeddings: DashScopeEmbeddings,
        collection_name: str,
        dense_dim: int,
    ):
        self.client = client
        self.embeddings = embeddings
        self.collection_name = collection_name
        self.dense_dim = dense_dim

    async def init_collection(self):
        """初始化对话记忆集合"""
        if await self.client.has_collection(self.collection_name):
            logger.info(f"对话记忆集合 {self.collection_name} 已存在")
            await self.client.load_collection(self.collection_name)
            return

        # 创建 Schema
        schema = self.client.create_schema(enable_dynamic_field=True)

        # 主键
        schema.add_field(
            field_name="id",
            datatype=DataType.VARCHAR,
            max_length=50,
            is_primary=True,
            auto_id=True,
        )

        # 用户标识
        schema.add_field(
            field_name="user_id", datatype=DataType.INT64, is_partition_key=True
        )

        # 会话标识
        schema.add_field(
            field_name="thread_id", datatype=DataType.VARCHAR, max_length=100
        )

        # 记忆类型
        schema.add_field(
            field_name="memory_type", datatype=DataType.VARCHAR, max_length=30
        )

        # 记忆内容
        schema.add_field(
            field_name="content",
            datatype=DataType.VARCHAR,
            max_length=65535,
            enable_analyzer=True,
            analyzer_params={"type": "chinese"},
        )

        # 关联的 summary_id
        schema.add_field(
            field_name="summary_id",
            datatype=DataType.VARCHAR,
            max_length=50,
            nullable=True,
        )

        # 重要性评分
        schema.add_field(field_name="importance", datatype=DataType.FLOAT)

        # 时间戳
        schema.add_field(field_name="created_at", datatype=DataType.INT64)
        schema.add_field(
            field_name="last_access_at",
            datatype=DataType.INT64,
            default_value=0,
        )

        # 向量字段
        schema.add_field(
            field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=self.dense_dim
        )

        # BM25 稀疏向量
        schema.add_field(
            field_name="sparse_vector", datatype=DataType.SPARSE_FLOAT_VECTOR
        )

        # 配置 BM25 内置函数
        bm25_function = Function(
            name="bm25_func",
            function_type=FunctionType.BM25,
            input_field_names=["content"],
            output_field_names=["sparse_vector"],
        )
        schema.add_function(bm25_function)

        # 配置索引
        index_params = self.client.prepare_index_params()

        index_params.add_index(
            field_name="vector",
            index_name="vector_index",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": 16, "efConstruction": 200},
        )

        index_params.add_index(
            field_name="sparse_vector",
            index_name="sparse_index",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
        )

        index_params.add_index(field_name="user_id", index_type="STL_SORT")
        index_params.add_index(field_name="thread_id", index_type="INVERTED")  # Milvus2.5 适配: VARCHAR 不能用 STL_SORT
        index_params.add_index(field_name="memory_type", index_type="INVERTED")
        index_params.add_index(field_name="importance", index_type="STL_SORT")
        index_params.add_index(field_name="last_access_at", index_type="STL_SORT")
        index_params.add_index(field_name="created_at", index_type="STL_SORT")

        # 创建集合
        await self.client.create_collection(
            collection_name=self.collection_name,
            schema=schema,
            index_params=index_params,
            properties={"partitionkey.isolation": True},
        )
        await self.client.load_collection(self.collection_name)

    async def get_dense_vector(self, query: str) -> List[float]:
        """生成稠密向量，带重试机制"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                dense_vector = await self.embeddings.aembed_query(query)
                break
            except Exception as e:
                logger.error(
                    f"Embedding API 调用失败 (尝试 {attempt + 1}/{max_retries}): {e}"
                )
                if attempt == max_retries - 1:
                    logger.error(f"Embedding API 最终失败，返回空结果")
                    return []
                else:
                    logger.warning(f"等待 1 秒后重试...")
                    await asyncio.sleep(1)
                    continue
        return dense_vector

    async def hybrid_retrieval_memories(
        self,
        query: str,
        user_id: int,
        summary_k: int,
        semantic_k: int,
        episodic_k: int,
        procedural_k: int,
    ) -> Dict[str, List[Dict]]:
        """混合检索，每种记忆类型并行检索、去重，返回召回的相似记忆内容"""

        memory_configs = {
            key: {"k": k, "filter": f"user_id == {user_id} and memory_type == '{key}'"}
            for key, k in {
                "summary": summary_k,
                "semantic": semantic_k,
                "episodic": episodic_k,
                "procedural": procedural_k,
            }.items()
            if k
        }

        dense_vector = await self.get_dense_vector(query)
        if not dense_vector:
            return {}

        async def search_memory_type(memory_type: str, config: dict):
            k = config["k"]
            filter_expr = config["filter"]

            try:
                dense_req = AnnSearchRequest(
                    data=[dense_vector],
                    anns_field="vector",
                    param={"metric_type": "COSINE", "params": {"ef": 64}},
                    limit=k * 6,
                    expr=filter_expr,
                )

                sparse_req = AnnSearchRequest(
                    data=[query],
                    anns_field="sparse_vector",
                    param={"metric_type": "BM25"},
                    limit=k * 4,
                    expr=filter_expr,
                )

                results = await self.client.hybrid_search(
                    collection_name=self.collection_name,
                    reqs=[dense_req, sparse_req],
                    ranker=RRFRanker(),
                    limit=k * 3,
                    output_fields=[
                        "id",
                        "memory_type",
                        "content",
                        "summary_id",
                        "importance",
                        "last_access_at",
                        "thread_id",  # P2：更新访问时间需带必填字段
                        "created_at",  # P2：upsert 全字段带齐
                        "vector",      # P2：向量含索引，upsert 必须带
                    ],
                )

                memories = []
                seen_ids = set()

                if results and len(results) > 0:
                    for hit in results[0]:
                        entity = hit["entity"]
                        memory_id = entity.get("id")

                        if memory_id and memory_id not in seen_ids:
                            seen_ids.add(memory_id)
                            memories.append(
                                {
                                    "id": memory_id,
                                    "memory_type": entity.get("memory_type"),
                                    "content": entity.get("content"),
                                    "summary_id": entity.get("summary_id"),
                                    "importance": entity.get("importance"),
                                    "last_access_at": entity.get("last_access_at"),
                                    "thread_id": entity.get("thread_id"),
                                    "created_at": entity.get("created_at"),
                                    "vector": entity.get("vector"),
                                    "score": hit["distance"],
                                }
                            )

                return {memory_type: memories[: k * 2]}

            except Exception as e:
                logger.error(
                    f"记忆类型 {memory_type} 混合检索失败：{e}, 降级为稠密向量检索"
                )

                try:
                    results = await self.client.search(
                        collection_name=self.collection_name,
                        data=[dense_vector],
                        anns_field="vector",
                        search_params={"metric_type": "COSINE", "params": {"ef": 64}},
                        limit=k * 6,
                        filter=filter_expr,
                        output_fields=[
                            "id",
                            "memory_type",
                            "content",
                            "summary_id",
                            "importance",
                            "last_access_at",
                            "thread_id",  # P2：同 hybrid 分支
                            "created_at",
                            "vector",
                        ],
                    )

                    memories = []
                    if results and len(results) > 0:
                        for hit in results[0]:
                            entity = hit["entity"]
                            memories.append(
                                {
                                    "id": entity.get("id"),
                                    "memory_type": entity.get("memory_type"),
                                    "content": entity.get("content"),
                                    "summary_id": entity.get("summary_id"),
                                    "importance": entity.get("importance"),
                                    "last_access_at": entity.get("last_access_at"),
                                    "thread_id": entity.get("thread_id"),
                                    "created_at": entity.get("created_at"),
                                    "vector": entity.get("vector"),
                                    "score": hit["distance"],
                                }
                            )

                    return {memory_type: memories[: k * 2]}

                except Exception as search_e:
                    logger.error(f"记忆类型 {memory_type} 稠密检索也失败：{search_e}")
                    return {memory_type: []}

        tasks = [
            search_memory_type(mem_type, config)
            for mem_type, config in memory_configs.items()
        ]

        all_results = await asyncio.gather(*tasks, return_exceptions=True)

        final_results = {
            "summary": [],
            "semantic": [],
            "episodic": [],
            "procedural": [],
        }

        for result in all_results:
            if isinstance(result, Exception):
                logger.error(f"并行检索任务失败：{result}")
                continue

            if result and isinstance(result, dict):
                for mem_type, memories in result.items():
                    if mem_type in final_results:
                        final_results[mem_type].extend(memories)

        top_k_memories = self.get_the_top_k_memories(
            memory_dict=final_results,
            memory_configs=memory_configs,
        )

        # 更新访问时间是次要功能；自建 Milvus 对 upsert 必填字段校验严格，
        # 失败时不影响检索结果（容错，不阻塞记忆注入）
        try:
            await self.update_memory_last_access_time(top_k_memories, user_id=user_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"更新记忆访问时间失败（不影响检索）: {e}")

        return top_k_memories
        """
        返回的结构类似下面这样
        {
        "summary": 
            {
                "id": "mem_001",#这里的id是记忆的id，方便后续更新记忆的last_access_at时使用
                "memory_type": "summary",
                "content": "用户喜欢喝美式咖啡，不加糖",
                "summary_id": "sum_001",或者None
                "last_access_at": 1734019200.0,  # 时间戳
            },
        "semantic": [],
        "episodic": [],
        "procedural": [],
        }
        """

    async def update_memory_last_access_time(
        self, top_k_memories: Dict[str, List[Dict]], user_id: int | None = None
    ):
        """更新记忆的最后访问时间（P2：Redis 节流 + Milvus upsert）。

        设计（面试可讲）：
          - Milvus 的 upsert 每次检索都要全字段（含 1024 维向量）写回，高频检索写放大严重；
          - 这里先用 Redis 记录触摸时间：同一记忆在 _TOUCH_TTL(默认 60s) 内只 upsert 一次，
            写放大降一个数量级；Redis 不可用时直接 upsert（保底，功能不丢）。
          - upsert 时带齐 Milvus 必填字段（thread_id/memory_type/content/created_at/vector…），
            避免 Milvus 逐个报 fieldSchema 缺字段。
        """
        if user_id is None:
            return
        now = int(time.time())
        redis = None
        try:
            redis = _get_redis()
        except Exception:  # noqa: BLE001
            redis = None

        to_update = []
        for memories in top_k_memories.values():
            for mem in memories:
                if "id" not in mem:
                    continue
                mid = str(mem["id"])
                if redis is not None:
                    try:
                        last = await redis.hget(f"mem_touch:{user_id}", mid)
                        if last is not None and now - int(last) < _TOUCH_TTL:
                            continue  # 节流：60s 内已更新过，跳过本次写放大
                        pipe = redis.pipeline()
                        pipe.hset(f"mem_touch:{user_id}", mid, now)
                        pipe.expire(f"mem_touch:{user_id}", _TOUCH_TTL * 10)
                        await pipe.execute()
                    except Exception:  # noqa: BLE001
                        pass  # Redis 抖动则不节流，直接 upsert 保底
                to_update.append(mem)

        if not to_update:
            return

        update_data = []
        for mem in to_update:
            update_data.append(
                {
                    "id": mem["id"],
                    "user_id": user_id,
                    "last_access_at": now,
                    "thread_id": mem.get("thread_id") or "",
                    "memory_type": mem.get("memory_type") or "",
                    "content": mem.get("content") or "",
                    "summary_id": mem.get("summary_id"),
                    "importance": mem.get("importance", 0.5),
                    "created_at": mem.get("created_at") or now,
                    "vector": mem.get("vector") or [],
                }
            )

        res = await self.client.upsert(
            collection_name=self.collection_name,
            data=update_data,
            partial_update=True,
        )
        logger.info("更新记忆访问时间（节流后 %d 条）: %s", len(update_data), res)

    async def prune_memories(
        self,
        user_id: int,
        max_age_days: float = 90.0,
        min_importance: float = 0.3,
    ) -> int:
        """P2：记忆库健康治理——淘汰「久未访问 + 低重要度」的记忆（LRU 近似）。

        面试可讲：记忆只增不减会让检索噪声越来越大（库里全是陈年低价值记忆）。
        这里按 last_access_at（时近性）和 importance（价值）双条件淘汰，
        配合后台定时任务，控制记忆库规模。
        """
        cutoff = time.time() - max_age_days * 86400
        expr = (
            f"user_id == {user_id} and "
            f"last_access_at < {cutoff} and importance < {min_importance}"
        )
        try:
            res = await self.client.delete(
                collection_name=self.collection_name, filter=expr
            )
            n = int(getattr(res, "delete_count", 0) or 0)
            if n:
                logger.info("记忆淘汰: 用户 %s 清理 %d 条低频低价值记忆", user_id, n)
            return n
        except Exception as e:  # noqa: BLE001
            logger.warning("记忆淘汰失败（不影响检索）: %s", e)
            return 0

    def get_the_top_k_memories(
        self,
        memory_dict: Dict[str, List[Dict]],
        alpha: float = 0.45,
        beta: float = 0.25,
        gamma: float = 0.3,
        memory_configs: Dict[str, Dict] = None,
        type_weights: Dict[str, float] = None,
        total_cap: int | None = None,
    ) -> Dict[str, List[Dict]]:
        """P2 修正：带类型权重的统一排序 + 每类型保底 + 总名额封顶。

        旧实现的问题（面试可讲）：每类型独立取 top_k，type_weight 是死配置——
          - 不改变名额（名额由 k 配额定）；
          - 也不改变同类型排序（同类型乘同一常数，相对顺序不变）。
        新设计：
          1) 每类型内部先按「基础分」排序（语义分 + 时近 + 重要度，不含类型权重）；
          2) 每类型**保底 1 条**（各自最高分）——四类都有覆盖，不会被挤没；
          3) 其余候选进「竞争池」（各类型配额内），带 type_weight 统一评分，
             **总名额封顶**（total_cap，默认 6）制造真实竞争——配额总和大于总名额时，
             「语义记忆最重要」的权重才真正决定谁入选。
        """
        if type_weights is None:
            type_weights = {
                "summary": 0.7,
                "semantic": 1.3,
                "episodic": 1.0,
                "procedural": 1.2,
            }
        memory_configs = memory_configs or {}
        if total_cap is None:
            total_cap = int(os.getenv("MEM_MAX_TOTAL_RECALL", "6"))
        current_time = time.time()
        DECAY_RATE = 0.995

        def _base_score(mem: Dict) -> float:
            # 注意：字段可能是显式 None（Milvus 未存），用 or 兜底而非 get 默认值
            semantic_score = mem.get("score") or 0.5
            last_access = mem.get("last_access_at") or current_time
            hours_passed = (current_time - last_access) / 3600
            recency_score = DECAY_RATE**hours_passed
            importance_score = mem.get("importance") or 0.5
            return (
                alpha * semantic_score
                + beta * recency_score
                + gamma * importance_score
            )

        # 每类型候选按基础分排序（不含类型权重）
        per_type: Dict[str, List[tuple]] = {
            mt: sorted(((m, _base_score(m)) for m in mems), key=lambda x: x[1], reverse=True)
            for mt, mems in memory_dict.items()
        }

        result: Dict[str, List[Dict]] = {mt: [] for mt in memory_dict}
        quota = {
            mt: int(memory_configs[mt]["k"])
            for mt in per_type
            if mt in memory_configs and memory_configs.get(mt, {}).get("k")
        }

        # 1) 保底：每类型最高分 1 条
        reserved = 0
        for mt, scored in per_type.items():
            if scored:
                result[mt].append(scored[0][0])
                per_type[mt] = scored[1:]
                reserved += 1

        # 2) 竞争池：各类型「配额内」的剩余候选，带类型权重统一评分；
        #    总名额封顶制造真实竞争（否则 pool 恒等于名额，权重无意义）
        total_quota = sum(quota.values())
        total_slots = max(1, min(total_quota, total_cap))
        remaining_slots = max(0, total_slots - reserved)
        pool = []
        for mt, scored in per_type.items():
            tw = type_weights.get(mt, 1.0)
            cap = max(0, quota.get(mt, 0) - 1)  # 保底已占 1，竞争名额上限 = k-1
            for mem, base in scored[:cap]:
                pool.append((mem, base * tw))  # 类型权重在这里生效
        pool.sort(key=lambda x: x[1], reverse=True)

        for mem, _ in pool[:remaining_slots]:
            mt = mem.get("memory_type") or "summary"
            result[mt].append(mem)

        return result

    async def resolve_conflicts(self, filtered_memory: dict, user_id: int) -> dict:
        """P2 修正：相似记忆不再「删新」，而是「以新替旧」。

        旧问题（面试可讲）：新记忆与旧记忆相似度>0.9 就删掉新记忆——但新记忆往往是
        用户**当前最新关注点**，删新留旧会丢时效（旧记忆越存越久越过时）。
        新设计：
          - 所有新提取的记忆都保留（它是"现在"）；
          - 相似度 >=0.95（几乎同一条）：记录旧记忆 id → 调用方「以新替旧」删除旧条，
            库不膨胀且内容是最新的；
          - 相似度 0.9~0.95（主题相关但可能不同）：两条都保留，交给综合排序/权重处理，
            避免误删（如"喜欢美式"vs"喜欢拿铁"是两条真实偏好）。
        返回 {"memory": 全部新记忆, "supersede_ids": [旧记忆id]}
        """
        items = []
        for key in ["semantic_memory", "episodic_memory", "procedural_memory"]:
            for idx, mem in enumerate(filtered_memory.get(key, [])):
                items.append((key, idx, mem["content"], key.replace("_memory", "")))
        if filtered_memory.get("summary"):
            items.append(
                ("summary", None, filtered_memory["summary"]["content"], "summary")
            )
        if not items:
            return {"memory": filtered_memory, "supersede_ids": []}

        vectors = await self.embeddings.aembed_documents([item[2] for item in items])

        from collections import defaultdict

        type_to_items = defaultdict(list)
        type_to_vectors = defaultdict(list)
        for item, vec in zip(items, vectors):
            type_to_items[item[3]].append(item)
            type_to_vectors[item[3]].append(vec)

        async def search_type(mem_type, type_items, type_vectors):
            filter_expr = f"user_id == {user_id} and memory_type == '{mem_type}'"
            results = await self.client.search(
                collection_name=self.collection_name,
                data=type_vectors,
                anns_field="vector",
                search_params={"metric_type": "COSINE", "params": {"ef": 64}},
                limit=1,
                filter=filter_expr,
                output_fields=["id"],
            )
            out = []
            for i in range(len(type_items)):
                if results and results[i]:
                    hit = results[i][0]
                    dist = float(hit.get("distance", 0.0) or 0.0)
                    old_id = hit.get("entity", {}).get("id")
                    out.append((dist, old_id))
                else:
                    out.append((0.0, None))
            return out

        tasks = [
            search_type(t, type_to_items[t], type_to_vectors[t]) for t in type_to_items
        ]
        all_results = await asyncio.gather(*tasks)

        results_flat = []
        for res in all_results:
            results_flat.extend(res)

        supersede_ids = []
        for (key, idx, _, _), (dist, old_id) in zip(items, results_flat):
            # 极高相似（几乎同一条）→ 以新替旧：新记忆保留，旧记忆记录待删除
            if dist >= 0.95 and old_id:
                supersede_ids.append(str(old_id))

        return {"memory": filtered_memory, "supersede_ids": supersede_ids}

    async def delete_memories(self, user_id: int, memory_ids: list) -> int:
        """P2：以新替旧——删除被新记忆替代的旧记忆（避免库膨胀）。"""
        if not memory_ids:
            return 0
        expr = f"user_id == {user_id} and id in {list(memory_ids)}"
        try:
            res = await self.client.delete(
                collection_name=self.collection_name, filter=expr
            )
            n = int(getattr(res, "delete_count", 0) or 0)
            if n:
                logger.info("以新替旧: 删除 %d 条被替代的旧记忆", n)
            return n
        except Exception as e:  # noqa: BLE001
            logger.warning("删除被替代旧记忆失败（不影响主流程）: %s", e)
            return 0

    async def count_memories(self, user_id: int) -> int:
        """统计该用户记忆总数（容量上限前置）。"""
        try:
            res = await self.client.query(
                collection_name=self.collection_name,
                filter=f"user_id == {user_id}",
                output_fields=["id"],
                limit=16384,
            )
            return len(res)
        except Exception as e:  # noqa: BLE001
            logger.warning("统计记忆数失败: %s", e)
            return 0

    async def list_memories(self, user_id: int, limit: int = 20) -> list:
        """列出该用户记忆（按最近访问倒序），供「我的记忆」/单条删除用。"""
        try:
            res = await self.client.query(
                collection_name=self.collection_name,
                filter=f"user_id == {user_id}",
                output_fields=[
                    "id", "memory_type", "content", "created_at",
                    "importance", "last_access_at",
                ],
                limit=limit,
                order_by="last_access_at desc",
            )
            return [
                {
                    "id": r.get("id"),
                    "memory_type": r.get("memory_type"),
                    "content": r.get("content"),
                    "importance": r.get("importance"),
                    "last_access_at": r.get("last_access_at"),
                    "created_at": r.get("created_at"),
                }
                for r in res
            ]
        except Exception as e:  # noqa: BLE001
            logger.warning("列出记忆失败: %s", e)
            return []

    async def prune_to_capacity(self, user_id: int, max_count: int = 200) -> int:
        """P2 记忆容量上限：超限时删掉最久未访问的，直到 <= max_count（精确 LRU 近似）。

        面试可讲：记忆库只增不减会失控——加"每人最多 N 条"硬上限，
        超了按 last_access_at 升序删最老（等价 LRU）。配合低频淘汰，双保险。
        """
        total = await self.count_memories(user_id)
        excess = total - max_count
        if excess <= 0:
            return 0
        try:
            oldest = await self.client.query(
                collection_name=self.collection_name,
                filter=f"user_id == {user_id}",
                output_fields=["id"],
                limit=excess,
                order_by="last_access_at asc",
            )
            ids = [r["id"] for r in oldest]
            if not ids:
                return 0
            return await self.delete_memories(user_id, ids)
        except Exception as e:  # noqa: BLE001
            logger.warning("容量淘汰失败（不影响检索）: %s", e)
            return 0

    async def add_memories_batch(
        self,
        user_id: int,
        thread_id: str,
        memory_dict: Dict[str, Any],
        summary_id: Optional[str] = None,
        **kwargs,
    ) -> bool:
        """批量添加记忆到 Milvus 集合"""
        created_at = kwargs.get("created_at", int(time.time()))
        last_access_at = kwargs.get("last_access_at", int(time.time()))

        texts_to_embed = []
        records_to_insert = []

        summary = memory_dict.get("summary", {})
        if summary and summary.get("content", "").strip():
            content = summary["content"].strip()
            texts_to_embed.append(content)
            records_to_insert.append(
                {
                    "user_id": user_id,
                    "thread_id": thread_id,
                    "memory_type": "summary",
                    "content": content,
                    "summary_id": summary_id,
                    "importance": float(summary.get("importance_score", 0.5)),
                    "created_at": created_at,
                    "last_access_at": last_access_at,
                }
            )

        type_mapping = {
            "semantic_memory": "semantic",
            "episodic_memory": "episodic",
            "procedural_memory": "procedural",
        }

        for key, memory_type in type_mapping.items():
            items = memory_dict.get(key, [])
            if not isinstance(items, list):
                continue

            for item in items:
                if isinstance(item, dict):
                    content = item.get("content", "").strip()
                    if content:
                        texts_to_embed.append(content)
                        records_to_insert.append(
                            {
                                "user_id": user_id,
                                "thread_id": thread_id,
                                "memory_type": memory_type,
                                "content": content,
                                "summary_id": None,
                                "importance": float(item.get("importance_score", 0.5)),
                                "created_at": created_at,
                                "last_access_at": last_access_at,
                            }
                        )

        if not texts_to_embed:
            logger.warning("没有有效内容需要插入")
            return False

        vectors = await self.embeddings.aembed_documents(texts_to_embed)

        for record, vector in zip(records_to_insert, vectors):
            record["vector"] = vector

        try:
            await self.client.insert(
                collection_name=self.collection_name,
                data=records_to_insert,
            )
            logger.info(
                f"批量添加记忆完成 - 总计：{len(records_to_insert)} 条，"
                f"用户：{user_id}, 会话：{thread_id}, summary_id: {summary_id}"
            )
            return True
        except Exception as e:
            logger.error(f"批量插入记忆失败：{e}")
            raise


async def test_update_access_time():
    """测试：更新记忆访问时间"""
    from milvus_client import get_milvus_client  # 根据实际导入路径调整

    client = AsyncMilvusClient(
        uri=os.getenv("Milvus_url"),
        token=os.getenv("Token"),
    )
    embeddings = DashScopeEmbeddings(
        model=os.getenv("EMBEDDING_MODEL"),
        dashscope_api_key=os.getenv("DASHSCOPE_API_KEY"),
    )

    memory_manager = MemoryManager(
        client=client,
        embeddings=embeddings,
        collection_name=os.getenv("memory_collection"),  # 你的集合名
        dense_dim=int(os.getenv("dense_dimension", "1024")),  # 你的向量维度
    )

    test_memory = {
        "semantic": [
            {
                "id": "465201085538812576",  # 这里的id是记忆的id
                "memory_type": "semantic",
                "content": "用户喜欢喝美式咖啡",
                "summary_id": None,
                "last_access_at": time.time(),
            },
        ]
    }

    await memory_manager.update_memory_last_access_time(test_memory)


if __name__ == "__main__":
    print("开始更新记忆访问时间")
    asyncio.run(test_update_access_time())
