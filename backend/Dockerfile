# ===== CodeMind Chat Sandbox 服务镜像（P0-F-1）=====
# 用法：
#   docker compose -f docker-compose.prod.yml up -d --build
#
# 面试可讲：
#   - 分层缓存：先 COPY requirements 装依赖层，再 COPY 代码 → 改代码不重装依赖
#   - 依赖锁文件 + PIP_NO_CACHE_DIR：镜像可复现、可减容
#   - 密钥不烧进镜像：不 COPY .env，靠 compose 环境变量注入
#   - 模型不烧进镜像：data/models 走 volume 只读挂载（bge-large-zh ~1.3G）
#
# 安全权衡（诚实讲）：服务本体为跑沙箱需要访问宿主 docker.sock（compose 挂载），
# 因此镜像内以 root 运行。真实生产应把"沙箱执行"拆成独立守护进程（更小权限面），
# 这里是单机最小可行方案（D-1 会进一步收窄攻击面）。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ---- 依赖层（变化频率低，单独一层利于缓存）----
COPY requirements.txt requirements-sandbox.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-sandbox.txt

# ---- 代码层 ----
COPY backend/ /app/backend/

WORKDIR /app/backend
EXPOSE 8020

# 服务本体（健康检查由 compose 配）
CMD ["uvicorn", "chat.server:app", "--host", "0.0.0.0", "--port", "8020"]
