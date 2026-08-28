"""任务队列接入对话图测试（B）。核心逻辑 _apply_task_request/_task_status_text 不碰 LLM，可单测。"""
import os
import uuid

import pytest

from chat.graph import TaskRequest, _apply_task_request, _task_status_text, build_graph
from tasks.manager import TaskManager

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6380/0")


def _uid():
    return f"test{uuid.uuid4().hex[:6]}"


@pytest.fixture(autouse=True)
async def _clean_queue():
    """每个测试前清空全局队列，防残留任务干扰其他测试文件。"""
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_URL)
    await r.delete("tasks:queue")
    await r.aclose()


@pytest.mark.asyncio
async def test_apply_create_enqueues():
    uid = _uid()
    mgr = TaskManager(user_id=uid)
    text = await _apply_task_request(
        mgr, TaskRequest(action="create", code="print('对话提交的任务')")
    )
    assert "任务号" in text
    task_id = text.split("`")[1]
    task = await mgr.get(task_id)
    assert task is not None and task.status.value == "queued"
    assert task.payload["code"] == "print('对话提交的任务')"


@pytest.mark.asyncio
async def test_apply_create_missing_code():
    uid = _uid()
    mgr = TaskManager(user_id=uid)
    text = await _apply_task_request(mgr, TaskRequest(action="create", code=""))
    assert "没识别到" in text


@pytest.mark.asyncio
async def test_status_queued():
    uid = _uid()
    mgr = TaskManager(user_id=uid)
    task = await mgr.create(task_type="code_exec", payload={"code": "x"})
    text = await _task_status_text(mgr, task.id)
    assert task.id in text and "排队中" in text


@pytest.mark.asyncio
async def test_status_missing():
    uid = _uid()
    mgr = TaskManager(user_id=uid)
    text = await _task_status_text(mgr, "deadbeef0000")
    assert "找不到" in text


@pytest.mark.asyncio
async def test_status_empty_id():
    uid = _uid()
    mgr = TaskManager(user_id=uid)
    text = await _task_status_text(mgr, "")
    assert "需要任务号" in text


@pytest.mark.asyncio
async def test_list_empty():
    uid = _uid()
    mgr = TaskManager(user_id=uid)
    text = await _apply_task_request(mgr, TaskRequest(action="list"))
    assert "你还没有任务" in text


@pytest.mark.asyncio
async def test_list_shows_tasks():
    uid = _uid()
    mgr = TaskManager(user_id=uid)
    task = await mgr.create(task_type="code_exec", payload={"code": "x"})
    text = await _apply_task_request(mgr, TaskRequest(action="list"))
    assert task.id in text and "queued" in text


def test_graph_compiles_with_task_node():
    g = build_graph()
    nodes = g.get_graph().nodes
    assert "task" in nodes
    # 路由表应包含 task 分支
    assert "task" in str(g.get_graph().edges)
