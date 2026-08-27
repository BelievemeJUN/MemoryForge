#!/bin/bash
# 全量冒烟回归（CodeMind 项目）
# 用法：./backend/test/run_all.sh
# 每个测试独立跑，输出到 /tmp/smoke_<名>.log；全部通过则退出 0。
set -u
cd "$(dirname "$0")/../.."   # 回到项目根

TESTS="sandbox_smoke chat_minimal_smoke chat_intent_smoke chat_exec_smoke verifier_smoke chat_selfheal_smoke chat_read_smoke"
FAIL=0

for t in $TESTS; do
  echo ""
  echo "========== $t =========="
  # 清掉持久 terminal 可能残留的环境变量（防 load_dotenv 读到旧值）
  unset user password host port db_name DATABASE_URL EMBEDDING_API_KEY \
        EMBEDDING_MODEL Milvus_url Token 2>/dev/null || true
  if ./.venv/bin/python "backend/test/$t.py" > "/tmp/smoke_$t.log" 2>&1; then
    echo "✅ $t 通过"
  else
    echo "❌ $t 失败 → 日志: /tmp/smoke_$t.log"
    tail -8 "/tmp/smoke_$t.log"
    FAIL=1
  fi
done

echo ""
if [ "$FAIL" -eq 0 ]; then
  echo "🎉 全部冒烟测试通过"
else
  echo "⚠️ 存在失败项"
fi
exit "$FAIL"
