"""MemoryForge 演示面板（P2/J）：对话 + 健康 + 成本 + 审计。

让「代码执行 Agent」的新能力看得见摸得着（面试演示用）。

用法：
    # 1. 先起后端
    cd backend && ../.venv/bin/uvicorn chat.server:app --port 8020
    # 2. 再起面板
    cd backend && ../.venv/bin/streamlit run ../frontend/dashboard.py
"""
import json
import os

import psycopg
import redis
import requests
import streamlit as st

DEFAULT_API = "http://localhost:8020"
DEFAULT_REDIS = "redis://localhost:6380/0"
DEFAULT_DSN = "postgresql://echomind:echomind123@localhost:5432/echomind_db?sslmode=disable"

# 品牌资源（无论 cwd 在哪都能定位）
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_LOGO_PATH = os.path.join(_PROJECT_ROOT, "assets", "logo.svg")

st.set_page_config(page_title="MemoryForge · 演示面板", page_icon="🧠", layout="wide")

# ---------- MemoryForge 品牌风格（自定义 CSS）----------
st.markdown(
    """
    <style>
      .mf-brand { text-align:center; padding: 8px 0 4px 0; }
      .mf-brand h1 { color:#312e81; font-size:2.2rem; margin:0; }
      .mf-brand p  { color:#64748b; font-size:0.95rem; margin:0; }
      div[data-testid="stMetric"] {
        background:#f8fafc; border:1px solid #e2e8f0; border-radius:12px; padding:8px 10px;
      }
      div[data-testid="stMetric"] label { color:#64748b; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------- 品牌 Header ----------
_brand_cols = st.columns([1, 6, 1])
with _brand_cols[1]:
    try:
        st.image(_LOGO_PATH, width=64)
    except Exception:  # noqa: BLE001  logo 缺失不阻塞面板
        pass
st.markdown(
    '<div class="mf-brand"><h1>🧠 MemoryForge</h1>'
    "<p>会写代码 · 能跑代码 · 会自我修复 · 记得住你的对话 Agent</p></div>",
    unsafe_allow_html=True,
)
st.divider()

# ---------- 侧边栏：连接配置 ----------
st.sidebar.title("⚙️ 连接配置")
api_base = st.sidebar.text_input("API 地址", DEFAULT_API)
api_key = st.sidebar.text_input("X-API-Key", "devkey1", type="password")
redis_url = st.sidebar.text_input("Redis", DEFAULT_REDIS)
pg_dsn = st.sidebar.text_input("PostgreSQL DSN", DEFAULT_DSN, type="password")

# ---------- 健康检查 ----------
st.header("🏥 依赖健康")
try:
    r = requests.get(f"{api_base}/health", timeout=5)
    data = r.json()
    st.success(f"整体状态: **{data['status']}**")
    deps = data.get("dependencies", {})
    cols = st.columns(max(1, len(deps)))
    for col, (name, info) in zip(cols, deps.items()):
        col.metric(name, "✅ 正常" if info.get("ok") else "❌ 异常")
except Exception as e:  # noqa: BLE001
    st.error(f"无法连接后端（请确认 server 已启动）: {e}")

st.divider()

# ---------- 对话 ----------
st.header("💬 对话 · 代码执行 Agent")
with st.form("chat_form"):
    msg = st.text_area("消息", "用python计算1加到100的和，只输出结果")
    thread = st.text_input("thread_id", "demo")
    go = st.form_submit_button("🚀 发送")

if go:
    with st.spinner("思考中…（意图判断 → 可能执行代码 → 返回）"):
        try:
            resp = requests.post(
                f"{api_base}/api/sandbox/chat",
                json={"message": msg, "thread_id": thread},
                headers={"X-API-Key": api_key},
                stream=True,
                timeout=180,
            )
            if resp.status_code != 200:
                st.error(f"HTTP {resp.status_code}: {resp.text}")
            else:
                full = ""
                with st.container(border=True):
                    for line in resp.iter_lines(decode_unicode=True):
                        if line and line.startswith("data: "):
                            try:
                                payload = json.loads(line[6:])
                            except json.JSONDecodeError:
                                continue
                            if payload.get("type") == "token":
                                full += payload.get("content", "")
                    st.markdown(full if full else "（空回复）")
        except Exception as e:  # noqa: BLE001
            st.error(f"请求失败: {e}")

st.divider()

# ---------- 成本看板 ----------
st.header("💰 成本看板（当日 token 用量）")
try:
    rc = redis.Redis.from_url(redis_url, socket_connect_timeout=3)
    # 只看当日成本 cost:YYYYMMDD:user（排除 cost:hist:user:YYYYMMDD 的 4 段快照）
    keys = sorted(
        k for k in rc.keys("cost:*") if len(k.decode().split(":")) == 3
    )
    if not keys:
        st.info("暂无成本记录（还没人对话过）")
    else:
        rows = []
        for k in keys:
            parts = k.decode().split(":")
            rows.append({"用户": parts[-1], "当日 token": int(rc.get(k) or 0)})
        st.dataframe(rows, width="stretch")

    # 成本趋势（近 7 日，来自 add_usage 的 hist 快照）
    st.markdown("##### 📈 近 7 日成本趋势（token/天）")
    try:
        from datetime import date, timedelta

        import pandas as pd

        hist_keys = sorted(rc.keys("cost:hist:*"))
        if not hist_keys:
            st.info("暂无历史成本（今天记账后开始积累，最多回看 31 天）")
        else:
            series: dict[str, dict[str, int]] = {}
            for k in hist_keys:
                parts = k.decode().split(":")
                # cost:hist:{user}:{YYYYMMDD}
                series.setdefault(parts[2], {})[parts[3]] = int(rc.get(k) or 0)
            today = date.today()
            dates = [
                (today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)
            ]
            df = pd.DataFrame(
                {
                    user: [day.get(d.replace("-", ""), 0) for d in dates]
                    for user, day in series.items()
                },
                index=dates,
            )
            st.line_chart(df)
    except Exception as e:  # noqa: BLE001
        st.error(f"趋势读取失败: {e}")
except Exception as e:  # noqa: BLE001
    st.error(f"Redis 不可用: {e}")

st.divider()

# ---------- 沙箱审计 ----------
st.header("📋 最近沙箱执行审计")
try:
    with psycopg.connect(pg_dsn, connect_timeout=3) as conn:
        rows = conn.execute(
            "SELECT user_id, code_len, exit_code, timed_out, security_blocked, "
            "ROUND(duration::numeric,2), created_at FROM sandbox_audit ORDER BY id DESC LIMIT 10"
        ).fetchall()
    if not rows:
        st.info("暂无审计记录（还没执行过代码）")
    else:
        st.dataframe(
            [
                {
                    "用户": r[0],
                    "代码长度": r[1],
                    "退出码": r[2],
                    "超时": r[3],
                    "被拦截": r[4],
                    "耗时(s)": r[5],
                    "时间": str(r[6]),
                }
                for r in rows
            ],
            width="stretch",
        )
except Exception as e:  # noqa: BLE001
    st.error(f"审计读取失败: {e}")
