"""LangGraph 对话状态定义（M2-1）。

面试可讲：
  - `messages` 用 `Annotated[list, add_messages]` 声明「增量合并」——每次节点返回
    新消息时，LangGraph 自动追加而不是覆盖，多轮对话历史就是这样累积起来的。
  - `total=False` 表示这些键可缺省，节点按需读写，状态图更灵活。
"""
from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages


class ChatState(TypedDict, total=False):
    """对话总图状态。

    字段说明：
      messages     全部对话消息（增量合并，历史自动累积）
      user_id      用户标识（M4 会话隔离会用 thread_id 真正隔离）
      thread_id    会话标识
      knowledge_base_id  当前知识库（M2-3 接读工具节点时启用）
    """

    messages: Annotated[list, add_messages] #追加而不是覆盖
    user_id: str
    thread_id: str
    knowledge_base_id: str
    intent: str  # M2-3 意图判断结果（chat/code/kb/memory）
