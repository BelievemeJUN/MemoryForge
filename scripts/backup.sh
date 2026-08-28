#!/bin/bash
# MemoryForge 数据备份（P2 数据合规）：PG + Redis + Milvus 说明
# 用法：./scripts/backup.sh
# 恢复演练：PG 用 psql 导入备份；Redis 用 .rdb 恢复；Milvus 用 milvus-backup 工具。
set -euo pipefail
TS=$(date +%Y%m%d_%H%M%S)
OUT="backups/$TS"
mkdir -p "$OUT"

echo "=== 备份到 $OUT ==="

# 1) PostgreSQL 全库 dump
echo "[1/3] PostgreSQL dump..."
docker exec agent-postgres pg_dump -U echomind -d echomind_db > "$OUT/postgres.sql"
echo "      -> $OUT/postgres.sql ($(du -h "$OUT/postgres.sql" | cut -f1))"

# 2) Redis RDB 快照（BGSAVE + 拷贝）
echo "[2/3] Redis RDB..."
docker exec codemind-redis redis-cli BGSAVE > /dev/null
sleep 2
docker cp codemind-redis:/data/dump.rdb "$OUT/redis.rdb" 2>/dev/null || echo "      (RDB 拷贝失败，可忽略——Redis 多为可重建缓存)"
echo "      -> $OUT/redis.rdb"

# 3) Milvus 说明（向量库推荐 milvus-backup 工具）
echo "[3/3] Milvus..."
echo "Milvus 建议用官方 milvus-backup 工具导出集合；或重启时依赖 docker/volumes 落盘。" \
     > "$OUT/milvus-README.txt"
echo "      -> $OUT/milvus-README.txt"

echo "=== 备份完成: $OUT ==="
echo "恢复演练：psql -U echomind -d echomind_db < backups/$TS/postgres.sql"
