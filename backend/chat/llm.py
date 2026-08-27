"""共享 LLM 构造（M2-4）。

把模型构造抽到独立模块，避免 chat.graph 与 workflow.exec_loop 循环导入。
"""
import os

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv()


def build_chat_model() -> ChatOpenAI:
    """构建对话模型（DeepSeek OpenAI 兼容接口）。"""
    return ChatOpenAI(
        model=os.getenv("AGENT_BASE_MODEL", "deepseek-chat"),
        openai_api_key=os.getenv("DASHSCOPE_API_KEY"),
        openai_api_base=os.getenv("BASE_URL", "https://api.deepseek.com"),
        temperature=0.3,
        streaming=True,
    )
