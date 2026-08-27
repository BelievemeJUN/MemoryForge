# CodeMind 沙箱标准模板镜像（P2）：python:3.12-slim + 常用数据分析库
#
# 为什么需要模板镜像（面试可讲）：
#   - 裸 python:3.12-slim 没有第三方库，用户让 Agent 算矩阵/读 CSV/数据分析会
#     import 失败——「功能缺口」；
#   - 预装常用库做成「模板」（E2B 的 Template 概念）：常用任务开箱即用，
#     而不用每次都在沙箱里 pip install（断网沙箱本来也装不了）；
#   - 安全：仍走 executor 的完整防护（断网/只读/非root/seccomp），
#     只是镜像里多几个纯 Python/C 库，攻击面增量可控。
#
# 构建：
#   docker build -t codemind-sandbox:std docker/sandbox/
FROM python:3.12-slim

# 预装常用数据分析库（按需扩充：numpy/pandas/scipy/requests 等）
RUN pip install --no-cache-dir numpy pandas

# 沙箱运行态由 executor 挂载入口脚本（/scripts/run_python.sh）驱动，
# 镜像本身不做任何网络/写盘配置（保持只读 + 断网）。
