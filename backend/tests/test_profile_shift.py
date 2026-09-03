"""A 方向测试：偏好句豁免 4000 阈值，低阈值仅画像更新。纯逻辑+mock，无 Docker。"""
import pytest

from auto_store_memory_from_psql import has_preference_shift


# ---------- 转折检测 ----------
def test_has_preference_shift_hits_user_explicit():
    msgs = [
        {"id": 1, "role": "user", "content": "你写吧，但是以后注释都改成中文的"},
        {"id": 2, "role": "assistant", "content": "好的"},
    ]
    assert has_preference_shift(msgs) is True


def test_has_preference_shift_hits_human_role():
    """真实库以 human/ai 存消息（chat/server.py），human 也要命中——否则 A 在真实数据不触发。"""
    msgs = [
        {"id": 1, "role": "human", "content": "以后我写的代码注释都用英文吧"},
        {"id": 2, "role": "ai", "content": "好的"},
    ]
    assert has_preference_shift(msgs) is True


def test_has_preference_shift_ignores_assistant_and_plain_chat():
    # assistant 里出现关键词不算；纯闲聊不算
    msgs = [
        {"id": 1, "role": "assistant", "content": "以后我都会用中文注释"},
        {"id": 2, "role": "user", "content": "今天天气不错"},
    ]
    assert has_preference_shift(msgs) is False


def test_has_preference_shift_empty():
    assert has_preference_shift([]) is False


# ---------- 低阈值画像更新（_profile_only_update 经由 extract_and_append_memory） ----------
class FakeResp:
    def __init__(self, content):
        self.content = content


class FakeModel:
    """记录调用次数，第二次返回 merge 结果。"""

    def __init__(self, extract_text, merge_text=None):
        self.extract_text = extract_text
        self.merge_text = merge_text
        self.calls = 0

    async def ainvoke(self, msgs):
        self.calls += 1
        if self.calls == 1:
            return FakeResp(self.extract_text)
        return FakeResp(self.merge_text or self.extract_text)


class FakePG:
    def __init__(self, old_profile=""):
        self.old_profile = old_profile
        self.updated_profile = None
        self.summary_written = None
        self.summary_rolled_back = []

    async def get_user_profile(self, user_id):
        return self.old_profile

    async def update_user_profile(self, user_id, profile):
        self.updated_profile = profile
        return True

    async def update_messages_with_summary_id(self, msg_ids, summary_id):
        if summary_id is None:
            self.summary_rolled_back.extend(msg_ids)
            return True
        self.summary_written = summary_id
        return True


def _make_msgs(texts):
    return [
        {"id": i, "role": "user", "content": t}
        for i, t in enumerate(texts, 1)
    ]


@pytest.mark.asyncio
async def test_low_threshold_with_shift_updates_profile(monkeypatch):
    """未达阈值 + 显式转折 → 提炼画像并 merge 进 PG；新画像同步写 semantic 记忆库，
    且对库中几乎同义(≥0.95)的旧偏好条目以新替旧删除（画像与语义记忆一致化）。"""
    import auto_store_memory_from_psql as m

    pg = FakePG(old_profile="用户偏好英文注释。")
    monkeypatch.setattr(m, "get_postgresql_client", lambda: _await(pg))

    class FakeMilvus:
        def __init__(self):
            self.added = None
            self.superseded = []

        async def resolve_conflicts(self, filtered_memory, user_id):
            return {"memory": filtered_memory, "supersede_ids": ["old_mem_1"]}

        async def add_memories_batch(self, **kw):
            self.added = kw
            return True

        async def delete_memories(self, user_id, ids):
            self.superseded = ids
            return len(ids)

    mc = FakeMilvus()
    monkeypatch.setattr(m, "get_milvus_client", lambda: _await(mc))

    model = FakeModel("用户现在偏好中文注释。", "用户偏好中文注释。")
    messages = _make_msgs(["以后注释都改成中文的吧，谢谢"])
    result = await m.extract_and_append_memory(messages, model, user_id=1, thread_id="t1")

    assert result["success"] is True
    assert result["mode"] == "profile_only"
    assert result["low_threshold_trigger"] is True
    assert result["user_profile"] == "用户偏好中文注释。"  # merge 结果入库
    assert pg.updated_profile == "用户偏好中文注释。"
    assert pg.summary_written is not None  # 打 summary_id 防重复
    assert model.calls == 2  # 提炼 + merge
    # 数据级同步：新画像作为 semantic 写入检索库，旧同义条目以新替旧删除
    assert mc.added["memory_dict"]["semantic_memory"][0]["content"] == "用户偏好中文注释。"
    assert mc.added["summary_id"] == pg.summary_written
    assert mc.superseded == ["old_mem_1"]


@pytest.mark.asyncio
async def test_low_threshold_shift_but_no_profile_extracted(monkeypatch):
    """命中转折词但模型判断无新偏好 → 不改任何东西、不打 summary_id。"""
    import auto_store_memory_from_psql as m

    pg = FakePG(old_profile="用户偏好英文注释。")
    monkeypatch.setattr(m, "get_postgresql_client", lambda: _await(pg))
    model = FakeModel("")  # 提炼为空

    messages = _make_msgs(["以后都别用红色了"])  # 命中"都别"但非画像级
    result = await m.extract_and_append_memory(messages, model, user_id=1, thread_id="t1")

    assert result["success"] is False
    assert "画像不变" in result["reason"]
    assert pg.updated_profile is None
    assert pg.summary_written is None


@pytest.mark.asyncio
async def test_low_threshold_no_shift_still_skips(monkeypatch):
    """未达阈值且无转折 → 维持原 skip 行为（model 不被动用）。"""
    import auto_store_memory_from_psql as m

    class BoomModel:
        async def ainvoke(self, msgs):
            raise AssertionError("无转折的短批不应调用 LLM")

    messages = _make_msgs(["今天天气不错", "是啊，适合散步"])
    result = await m.extract_and_append_memory(messages, BoomModel(), user_id=1, thread_id="t1")
    assert result["success"] is False
    assert "未超过阈值" in result["reason"]


@pytest.mark.asyncio
async def test_above_threshold_still_full_extract(monkeypatch):
    """达阈值 → 仍走全量记忆提取（不回归）；转折不触发 profile_only。"""
    import auto_store_memory_from_psql as m

    async def _fake_extract(messages, model):
        raise RuntimeError("模拟全量提取内部失败")  # 让路径走失败分支，便于断言走了全量

    monkeypatch.setattr(m, "extract_memories", _fake_extract)
    monkeypatch.setattr(
        m, "count_tokens_approximately", lambda msgs: 99999
    )  # 强行超过阈值

    messages = _make_msgs(["以后都用中文注释吧"] + ["内容" * 100] * 3)
    result = await m.extract_and_append_memory(messages, FakeModel("x"), user_id=1, thread_id="t1")
    assert result["success"] is False
    assert "记忆提取失败" in result["reason"]  # 走了全量 extract 而非 profile_only
    assert "low_threshold_trigger" not in result


def _await(x):
    """把普通对象包成可 await（async def get_postgresql_client 的替代）。"""
    class _A:
        def __init__(self, v):
            self.v = v

        def __await__(self):
            yield
            return self.v

    return _A(x)
