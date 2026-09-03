"""
定时自动压缩psql中的历史对话，压缩函数提取的是langchain中的SummarizationMiddleware，
压缩时检查对话是否超过4000个token，超过则压缩，否则不压缩。
新增逻辑：大模型识别后半部分语义差异大且不完整的消息，仅压缩前半部分相关内容，返回过滤的消息ID列表
"""

import asyncio
import logging
import uuid
import json
from typing import List, Dict, Any
from datetime import datetime
import config
from langchain_core.messages import HumanMessage
from langchain_core.language_models import BaseChatModel
from langchain_core.messages.utils import count_tokens_approximately
from postgresql_client import get_postgresql_client
from langchain_openai import ChatOpenAI
from milvus_client import get_milvus_client
import os

logger = logging.getLogger(__name__)

TOKEN_THRESHOLD = 4000  # P2 修正：原 1000 太敏感（稍长对话就触发 LLM 提取烧钱），注释却写 10000，不一致。4000 兼顾提取及时性与成本
MEMORY_EXTRACT_PROMPT = config.MEMORY_EXTRACT_PROMPT
USER_PROFILE_MERGE_PROMPT = config.USER_PROFILE_MERGE_PROMPT
USER_PROFILE_ONLY_PROMPT = config.USER_PROFILE_ONLY_PROMPT

# A：显式偏好转折信号词——命中即豁免 TOKEN_THRESHOLD，触发"仅画像"低阈值更新。
# 关键洞察：风格转折常藏在一两句短消息里，攒不够 4000 token 就永远不触发压缩，
# 画像会长期滞后。这些词是"用户主动声明偏好变化"的高信号。
PREFERENCE_SHIFT_KEYWORDS = (
    "以后", "从今往后", "接下来都", "都改成", "改成", "改用", "换成",
    "都别", "别再", "不要再", "不要再用", "别用", "别",
    "请记住", "记住", "我一直", "我不喜欢", "我习惯", "每次都要",
    "请你以后", "从现在起", "以后不要", "偏好",
)


def has_preference_shift(messages: List[Dict[str, Any]]) -> bool:
    """检测这批消息里是否有「用户显式声明偏好/习惯变化」的句子（纯规则，不调 LLM）。

    A 方向的入口判断：命中 → 即使未达 4000 token 也做一次轻量画像更新。
    误报成本低（多一次短 LLM 调用），因为最终画像是否真变由 LLM 判断（无则不动）。
    """
    user_roles = {"user", "human"}  # 真实库以 human/ai 存（chat/server.py）；测试常用 user/assistant
    for m in messages:
        if str(m.get("role", "")).lower() not in user_roles:
            continue
        text = str(m.get("content", ""))
        for kw in PREFERENCE_SHIFT_KEYWORDS:
            if kw in text:
                return True
    return False


async def extract_memories(
    messages: List[Dict[str, Any]],
    model: BaseChatModel,
) -> Dict[str, Any]:
    """
    从对话中提取多种类型的记忆

    Args:
        messages: 原始消息列表（包含id/role/content/created_at）
        model: 用于提取记忆的 LLM 模型

    Returns:
        dict: {
            "summary": {"content": str, "importance_score": float},
            "semantic_memory": [{"content": str, "importance_score": float}, ...],
            "episodic_memory": [{"content": str, "importance_score": float}, ...],
            "procedural_memory": [{"content": str, "importance_score": float}, ...],
            "user_profile": str,
            "filtered_message_ids": list
        }
    """
    if not messages:
        return {
            "summary": {"content": "", "importance_score": 0.0},
            "semantic_memory": [],
            "episodic_memory": [],
            "procedural_memory": [],
            "user_profile": "",
            "filtered_message_ids": [],
        }

    # 格式化对话文本，每个消息占一行，格式为：id | role | content | time
    lines = []
    for msg in messages:
        lines.append(
            f"{msg.get('id', '')} | {msg.get('role', '')} | {msg.get('content', '')} | {msg.get('created_at', 'N/A')}"
        )
    conversation_text = "\n\n".join(lines)

    # 调用模型
    response = await model.ainvoke(
        [
            HumanMessage(
                content=MEMORY_EXTRACT_PROMPT.format(
                    conversation_text=conversation_text
                )
            )
        ]
    )

    content = response.content.strip()
    if content.startswith("```json"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]

    result = json.loads(content.strip())

    # 解析结果
    def safe_score(v):
        try:
            return round(
                max(0.0, min(1.0, float(v))), 1
            )  # 将importance_score安全地限制在 0.0 到 1.0 之间，并保留 1 位小数
        except:
            return 0.5

    # 解析记忆项，确保 content 字段存在且非空字符串
    def parse_items(items):
        if not isinstance(items, list):
            return []
        return [
            {
                "content": item["content"].strip(),
                "importance_score": safe_score(item.get("importance_score", 0.5)),
            }
            for item in items
            if isinstance(item, dict)
            and item.get("content", "").strip()  # 确保 content 字段存在且非空字符串
        ]

    summary_raw = result.get("summary", {})

    return {
        "summary": {
            "content": summary_raw.get("content", "").strip(),
            "importance_score": safe_score(summary_raw.get("importance_score", 0.5)),
        },
        "semantic_memory": parse_items(result.get("semantic_memory")),
        "episodic_memory": parse_items(result.get("episodic_memory")),
        "procedural_memory": parse_items(result.get("procedural_memory")),
        "user_profile": result.get("user_profile", "").strip(),
        "filtered_message_ids": result.get("filtered_message_ids", []),
    }


async def get_unsunmarized_conversations(
    user_id: int, thread_id: str
) -> List[Dict[str, Any]]:
    """获取指定用户和会话中未摘要的对话消息"""
    try:
        pg_client = await get_postgresql_client()

        if not pg_client.pool:
            logger.error("数据库连接池未初始化，无法获取未摘要消息")
            return []

        async with pg_client.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, role, content, created_at, thread_id
                FROM raw_conversations
                WHERE user_id = $1 
                AND thread_id = $2
                AND (summary_id IS NULL OR summary_id = '')
                ORDER BY created_at ASC
            """,
                user_id,
                thread_id,
            )

            messages = [dict(row) for row in rows]
            logger.info(
                f"获取到用户 {user_id} 会话 {thread_id} 的 {len(messages)} 条未摘要消息"
            )
            return messages

    except Exception as e:
        logger.error(
            f"获取未摘要消息失败 user_id={user_id}, thread_id={thread_id}: {e}",
            exc_info=True,
        )
        return []


# 这个才是存入记忆的关键
async def extract_and_append_memory(
    messages: List[Dict[str, Any]], model: BaseChatModel, user_id: int, thread_id: str
) -> Dict[str, Any]:
    """压缩单个会话的消息并生成摘要"""
    if not messages:
        return {"success": False, "reason": "没有消息需要压缩"}

    # 1. 计算 token 数
    conversation_text = "\n\n".join(
        [f"{m.get('role')}: {m.get('content')}" for m in messages]
    )
    total_tokens = count_tokens_approximately([HumanMessage(content=conversation_text)])

    if total_tokens <= TOKEN_THRESHOLD:
        # A：显式偏好转折（如“以后都用中文注释”）豁免 4000 门槛 → 仅画像低阈值更新。
        # 否则风格转折藏在短会话里攒不够阈值就永远不触发压缩，画像长期滞后。
        if has_preference_shift(messages):
            try:
                result = await _profile_only_update(messages, model, user_id, thread_id)
                result["token_count"] = total_tokens
                result["message_count"] = len(messages)
                result["low_threshold_trigger"] = True
                return result
            except Exception as e:  # noqa: BLE001
                logger.error(f"偏好句低阈值画像更新失败: {e}", exc_info=True)
                # 失败降级回原 skip（不阻塞压缩任务）
        return {
            "success": False,
            "reason": f"token 数未超过阈值 ({total_tokens} <= {TOKEN_THRESHOLD})",
            "token_count": total_tokens,
            "message_count": len(messages),
        }

    # 2. 提取记忆
    try:
        extract_result = await extract_memories(messages, model)
    except Exception as e:
        logger.error(f"记忆提取失败: {e}", exc_info=True)
        return {
            "success": False,
            "reason": f"记忆提取失败: {str(e)}",
            "token_count": total_tokens,
            "message_count": len(messages),
        }

    # 3. 存储记忆
    filtered_message_ids = extract_result.get("filtered_message_ids", [])
    try:
        return await _store_memories(
            messages=messages,
            extract_result=extract_result,
            user_id=user_id,
            thread_id=thread_id,
            total_tokens=total_tokens,
            filtered_message_ids=filtered_message_ids,
            model=model,
        )
    except Exception as e:
        logger.error(f"存储记忆失败: {e}", exc_info=True)
        return {
            "success": False,
            "reason": f"存储记忆失败: {str(e)}",
            "filtered_message_ids": filtered_message_ids,
            "token_count": total_tokens,
            "message_count": len(messages),
        }


async def _profile_only_update(
    messages: List[Dict[str, Any]], model: BaseChatModel, user_id: int, thread_id: str
) -> Dict[str, Any]:
    """A：仅画像的低阈值更新——不写 Milvus 记忆、不生成摘要，只提炼并 merge 画像。

    触发：消息总量 < 4000 token，但用户显式声明偏好转折（has_preference_shift 命中）。
    轻量一次 LLM 调用提炼画像增量 → merge 进 PG 画像 → 打 summary_id（防重复处理）。
    提炼为空 → 不改任何东西（诚实：模型判断这批没有新偏好）。
    """
    pg_client = await get_postgresql_client()

    conversation_text = "\n\n".join(
        f"{m.get('role')}: {m.get('content')}" for m in messages
    )
    resp = await model.ainvoke(
        [
            HumanMessage(
                content=USER_PROFILE_ONLY_PROMPT.format(messages=conversation_text)
            )
        ]
    )
    new_profile = (resp.content or "").strip()
    if not new_profile:
        return {"success": False, "reason": "未识别到明确偏好转折（画像不变）"}

    summary_id = str(uuid.uuid4())
    update_message_ids = [str(msg["id"]) for msg in messages if msg.get("id")]

    old_user_profile = await pg_client.get_user_profile(user_id)
    if old_user_profile:
        merged = await model.ainvoke(
            [
                HumanMessage(
                    content=USER_PROFILE_MERGE_PROMPT.format(
                        old_user_profile=old_user_profile,
                        new_user_profile=new_profile,
                    )
                )
            ]
        )
        new_profile = (merged.content or "").strip()

    try:
        # 先标记这批已处理（防整批再提取重复），再更新画像；画像失败则回滚标记
        mark_ok = True
        if update_message_ids:
            mark_ok = await pg_client.update_messages_with_summary_id(
                update_message_ids, summary_id
            )
        if not mark_ok:
            return {"success": False, "reason": "summary_id 更新失败"}
        ok = await pg_client.update_user_profile(user_id, new_profile)
        if not ok:
            if update_message_ids:
                await pg_client.update_messages_with_summary_id(
                    update_message_ids, None
                )
            return {"success": False, "reason": "画像更新失败，已回滚标记"}
        logger.info(
            "A 低阈值画像更新 user=%s thread=%s new=%s",
            user_id, thread_id, new_profile[:60],
        )
        return {
            "success": True,
            "reason": f"低阈值画像更新成功 (profile_only, {len(messages)} 条)",
            "user_profile": new_profile,
            "mode": "profile_only",
            "filtered_message_ids": [],
        }
    except Exception as e:  # noqa: BLE001
        logger.error(f"画像更新异常: {e}", exc_info=True)
        return {"success": False, "reason": f"画像更新异常: {e}"}


async def _store_memories(
    messages: List[Dict[str, Any]],
    extract_result: Dict[str, Any],
    user_id: int,
    thread_id: str,
    total_tokens: int,
    filtered_message_ids: List[str],
    model: BaseChatModel,
) -> Dict[str, Any]:
    """存储记忆到 PostgreSQL 和 Milvus，任何一项失败都不提交数据"""
    milvus_client = await get_milvus_client()
    pg_client = await get_postgresql_client()

    new_user_profile = extract_result.get("user_profile", "")
    summary_id = str(uuid.uuid4())

    # 查询旧画像（用于回滚和画像补充）
    old_user_profile = await pg_client.get_user_profile(user_id)

    # 确定需要更新的消息ID
    all_message_ids = [str(msg["id"]) for msg in messages]
    update_message_ids = [
        mid for mid in all_message_ids if mid not in filtered_message_ids
    ]

    if not update_message_ids:
        logger.warning(f"会话 {thread_id} 所有消息都被过滤")
        return {
            "success": False,
            "reason": "所有消息都被过滤",
            "token_count": total_tokens,
            "message_count": len(messages),
        }

    # 准备数据
    keep_keys = ["summary", "semantic_memory", "episodic_memory", "procedural_memory"]
    filtered_memory = {k: v for k, v in extract_result.items() if k in keep_keys}
    """
    filtered_memory =  {
            "summary": {"content": str, "importance_score": float},
            "semantic_memory": [{"content": str, "importance_score": float}, ...],
            "episodic_memory": [{"content": str, "importance_score": float}, ...],
            "procedural_memory": [{"content": str, "importance_score": float}, ...],
            "user_profile": str,
            "filtered_message_ids": list
        }
    """
    # 检测冲突记忆（P2 修正）：不再删新记忆，而是返回被替代的旧记忆 id（以新替旧，保时效）
    conflict = await milvus_client.resolve_conflicts(
        filtered_memory=filtered_memory, user_id=user_id
    )
    filtered_memory = conflict["memory"]
    supersede_ids = conflict.get("supersede_ids", [])

    #使用小模型对新旧用户画像进行补充
    if new_user_profile:
        response = await model.ainvoke(
            [
                HumanMessage(
                    content=USER_PROFILE_MERGE_PROMPT.format(
                        old_user_profile=old_user_profile,
                        new_user_profile=new_user_profile,
                    )
                )
            ]
        )
        # 只需要这一行！纯文本画像直接用
        new_user_profile = response.content.strip()

    try:
        # 1. 更新 summary_id
        summary_success = await pg_client.update_messages_with_summary_id(
            update_message_ids, summary_id
        )
        if not summary_success:
            logger.error(f"summary_id 更新失败，终止操作")
            return {
                "success": False,
                "reason": "summary_id 更新失败",
                "filtered_message_ids": filtered_message_ids,
                "token_count": total_tokens,
                "message_count": len(messages),
            }

        # 2. 更新用户画像
        if new_user_profile:
            profile_success = await pg_client.update_user_profile(
                user_id, new_user_profile
            )
            if not profile_success:
                logger.error(f"用户画像更新失败，回滚 summary_id")
                await pg_client.update_messages_with_summary_id(
                    update_message_ids, None
                )
                return {
                    "success": False,
                    "reason": "用户画像更新失败，已回滚",
                    "filtered_message_ids": filtered_message_ids,
                    "token_count": total_tokens,
                    "message_count": len(messages),
                }

        # 3. 更新 Milvus
        milvus_success = await milvus_client.add_memories_batch(
            user_id=user_id,
            thread_id=thread_id,
            memory_dict=filtered_memory,
            summary_id=summary_id,
        )

        if not milvus_success:
            logger.error(f"Milvus插入失败，回滚PostgreSQL")
            # 回滚 summary_id
            await pg_client.update_messages_with_summary_id(update_message_ids, None)
            # 回滚用户画像
            if old_user_profile is not None:
                await pg_client.update_user_profile(user_id, old_user_profile)
            return {
                "success": False,
                "reason": "Milvus插入失败，已回滚PostgreSQL",
                "filtered_message_ids": filtered_message_ids,
                "token_count": total_tokens,
                "message_count": len(messages),
            }

        # P2 以新替旧：删除被新记忆替代的旧记忆（避免库膨胀，失败不影响主流程）
        if supersede_ids:
            await milvus_client.delete_memories(user_id, supersede_ids)

        # 全部成功
        logger.info(f"成功压缩会话 {thread_id}，摘要 {summary_id}")
        return {
            "success": True,
            "summary_id": summary_id,
            "user_profile": new_user_profile,
            "filtered_message_ids": filtered_message_ids,
            "token_count": total_tokens,
            "message_count": len(messages),
            "updated_message_count": len(update_message_ids),
        }

    except Exception as e:
        logger.error(f"存储记忆异常: {e}")

        # 尝试回滚
        try:
            await pg_client.update_messages_with_summary_id(update_message_ids, None)
            if old_user_profile is not None:
                await pg_client.update_user_profile(user_id, old_user_profile)
        except Exception as rollback_error:
            logger.error(f"回滚失败: {rollback_error}")

        return {
            "success": False,
            "reason": f"存储记忆失败: {str(e)}",
            "filtered_message_ids": filtered_message_ids,
            "token_count": total_tokens,
            "message_count": len(messages),
        }


async def process_all_users_conversations(
    model: BaseChatModel,
) -> Dict[str, Any]:
    """处理所有用户的未摘要对话（后台任务，不抛出异常）"""
    try:
        pg_client = await get_postgresql_client()

        if not pg_client.pool:
            logger.error("数据库连接池未初始化，无法执行压缩任务")
            return {
                "total_conversations_processed": 0,
                "compressed_count": 0,
                "skipped_count": 0,
                "failed_count": 0,
                "error": "数据库连接池未初始化",
                "details": [],
            }

        results = {
            "total_conversations_processed": 0,
            "compressed_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "details": [],
        }

        # 获取所有未摘要的会话
        async with pg_client.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT user_id, thread_id
                FROM raw_conversations
                WHERE summary_id IS NULL
                ORDER BY user_id, thread_id
            """
            )

        # 处理每个会话，捕获单个会话的异常
        for row in rows:
            user_id = row["user_id"]
            thread_id = row["thread_id"]

            try:
                # 获取未摘要的会话消息
                messages = await get_unsunmarized_conversations(user_id, thread_id)
                if messages:
                    # 压缩会话消息
                    result = await extract_and_append_memory(
                        messages, model, user_id, thread_id
                    )
                    results["total_conversations_processed"] += 1

                    if result.get("success"):
                        results["compressed_count"] += 1
                    elif "未超过阈值" in result.get("reason", ""):
                        results["skipped_count"] += 1
                    else:
                        results["failed_count"] += 1

                    results["details"].append(
                        {"user_id": user_id, "thread_id": thread_id, **result}
                    )
            except Exception as e:
                # 单个会话处理失败，记录错误但继续处理其他会话
                logger.error(
                    f"处理会话失败 user_id={user_id}, thread_id={thread_id}: {e}",
                    exc_info=True,
                )
                results["failed_count"] += 1
                results["details"].append(
                    {
                        "user_id": user_id,
                        "thread_id": thread_id,
                        "success": False,
                        "reason": f"处理异常: {str(e)}",
                        "filtered_message_ids": [],
                    }
                )

        # 输出汇总信息
        logger.info("=" * 60)
        logger.info("压缩任务完成汇总:")
        logger.info(f"  处理会话数: {results['total_conversations_processed']}")
        logger.info(f"  成功压缩数: {results['compressed_count']}")
        logger.info(f"  跳过（未超阈值）: {results['skipped_count']}")
        logger.info(f"  失败数: {results['failed_count']}")
        logger.info("=" * 60)

        return results

    except Exception as e:
        # 捕获整个任务的致命错误
        logger.error(f"压缩任务执行失败: {e}", exc_info=True)
        return {
            "total_conversations_processed": 0,
            "compressed_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "error": str(e),
            "details": [],
        }


async def run_prune_task(max_age_days: float = 90.0, min_importance: float = 0.3) -> int:
    """P2：记忆健康维护——遍历有对话记录的用户，淘汰低频低价值记忆 + 容量上限收紧。"""
    try:
        pg_client = await get_postgresql_client()
        if not pg_client.pool:
            return 0
        async with pg_client.pool.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT user_id FROM raw_conversations")

        from milvus_client import get_milvus_client  # lazy

        milvus = await get_milvus_client()
        # P2 记忆容量上限：每人最多 N 条（超了删最久未访问，近似 LRU）
        max_per_user = int(os.getenv("MEM_MAX_PER_USER", "200"))
        total = 0
        for row in rows:
            total += await milvus.memory_manager.prune_memories(
                row["user_id"],
                max_age_days=max_age_days,
                min_importance=min_importance,
            )
            total += await milvus.prune_to_capacity(row["user_id"], max_per_user)
        if total:
            logger.info("记忆维护: 共淘汰 %d 条（低频低价值 + 超容量）", total)
        return total
    except Exception as e:  # noqa: BLE001
        logger.warning("记忆维护任务失败（不影响对话）: %s", e)
        return 0


async def run_compression_task(model: BaseChatModel):
    """定时任务入口函数（兼容user_id参数，后台任务不抛出错误）"""
    logger.info("开始执行记忆存储任务...")
    start_time = datetime.now()

    try:
        pg_client = await get_postgresql_client()
        await pg_client.init_pool()

        # 兼容原参数（实际未使用user_id，保持接口一致）
        results = await process_all_users_conversations(model)

        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        logger.info(f"记忆存储任务完成，耗时: {duration:.2f} 秒")

        return results
    except Exception as e:
        logger.error(f"记忆存储任务执行失败: {e}", exc_info=True)
        return {
            "total_conversations_processed": 0,
            "compressed_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "error": str(e),
            "details": [],
        }


# ============ 使用示例 ============
if __name__ == "__main__":
    import sys

    sys.path.append(".")

    async def main():
        summarize_model = ChatOpenAI(
            model=os.getenv("SUMMARIZATION_MODEL", "qwen-turbo"),
            openai_api_key=os.getenv("DASHSCOPE_API_KEY"),
            openai_api_base=os.getenv("BASE_URL"),
            temperature=0.5,  # 总结模型温度，控制总结对话的随机性，0-1之间，0越确定，1越随机
        )

        results = await run_compression_task(summarize_model)
        print(results)
        print("\n压缩结果:")
        for detail in results["details"]:
            if detail.get("success"):
                filter_count = len(detail.get("filtered_message_ids", []))
                print(
                    f"  ✅ 用户 {detail['user_id']}, 会话 {detail['thread_id'][:20]}...: "
                    f"压缩 {detail['message_count']} 条消息 (过滤 {filter_count} 条) | {detail['token_count']} tokens"
                )
            elif "未超过阈值" in detail.get("reason", ""):
                print(
                    f"  ⏭️  用户 {detail['user_id']}, 会话 {detail['thread_id'][:20]}...: "
                    f"跳过 ({detail['token_count']} tokens)"
                )

    asyncio.run(main())
