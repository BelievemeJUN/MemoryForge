"""往知识库灌测试数据（M2-3 后半数据验证 + 演示用）。

直接运行：
    ./.venv/bin/python backend/test/seed_kb_data.py

流程：PostgreSQL 插 file_metadata + parent_chunks（外键前提）→ Milvus 插子块
（本地 bge 向量化）→ 检索验证。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
from langchain_core.documents import Document

load_dotenv(override=True)

from milvus_client import get_milvus_client  # noqa: E402
from postgresql_client import get_postgresql_client  # noqa: E402

KB_ID = "默认知识库"
USER_ID = 1

# (parent_id, file_hash, file_name, 内容)
DOCS = [
    (
        "p_rep_1",
        "h_rep_flow",
        "报销流程.txt",
        "公司的报销流程是：员工先填写报销申请单，注明费用类别和金额，然后提交给直属主管审批。"
        "审批通过后由财务部门在 3 个工作日内打款。",
    ),
    (
        "p_rep_2",
        "h_rep_flow",
        "报销流程.txt",
        "公司出差报销标准：住宿费每晚不超过 400 元，市内交通费实报实销，餐费每天 100 元。"
        "超支部分需要额外说明。",
    ),
    (
        "p_tech_1",
        "h_tech",
        "技术栈说明.txt",
        "公司后端使用 Python 和 FastAPI 框架开发，数据库用 PostgreSQL 和 Redis，向量检索用 Milvus。",
    ),
]


async def main():
    pg = await get_postgresql_client()
    milvus = await get_milvus_client()

    print("=== 0. 确保 users + knowledge_bases 存在（外键前提）===")
    await pg.pool.execute(
        "INSERT INTO users (user_id, created_at) VALUES ($1, CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING",
        USER_ID,
    )
    await pg.pool.execute(
        "INSERT INTO knowledge_bases (knowledge_base_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        KB_ID, USER_ID,
    )
    print("   users / knowledge_bases 已就绪")

    print("=== 1. PostgreSQL 插 file_metadata + parent_chunks ===")
    for parent_id, fhash, fname, text in DOCS:
        await pg.pool.execute(
            """INSERT INTO file_metadata (file_hash, file_name, knowledge_base_id, user_id)
               VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING""",
            fhash, fname, KB_ID, USER_ID,
        )
        await pg.pool.execute(
            """INSERT INTO parent_chunks
               (parent_id, knowledge_base_id, user_id, text, file_name, file_hash)
               VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING""",
            parent_id, KB_ID, USER_ID, text, fname, fhash,
        )
    print("   PostgreSQL 父块已写入")

    print("=== 2. Milvus 插子块（本地 bge 向量化）===")
    docs = [
        Document(
            page_content=t,
            metadata={
                "parent_id": pid,
                "file_hash": fh,
                "knowledge_base_id": KB_ID,
                "user_id": USER_ID,
            },
        )
        for pid, fh, _, t in DOCS
    ]
    await milvus.add_chunks_batch(KB_ID, docs, USER_ID)
    print("   Milvus 子块已写入")

    print("=== 3. 检索验证 ===")
    for q in ("公司报销流程是什么", "出差住宿标准是多少", "公司用什么后端框架"):
        ids = await milvus.hybrid_retrieval_knowledge_base(
            q, knowledge_base_id=KB_ID, top_k=3, user_id=USER_ID
        )
        print(f"   查询「{q}」→ 命中父块: {ids}")

    await pg.pool.close()
    await milvus.close()
    print("\n✅ 数据灌入 + 检索验证完成")


if __name__ == "__main__":
    asyncio.run(main())
