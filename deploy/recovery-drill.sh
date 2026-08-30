#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  echo "Usage: $0 [--full-oss] <backup.sql.gz> [report.json]" >&2
}

full_oss=0
if [[ "${1:-}" == "--full-oss" ]]; then
  full_oss=1
  shift
fi
if [[ $# -lt 1 || $# -gt 2 ]]; then
  usage
  exit 2
fi

backup_input=$1
report_input=${2:-}
if [[ ! -f "$backup_input" || "$backup_input" != *.sql.gz ]]; then
  echo "Backup must be an existing .sql.gz file" >&2
  exit 2
fi
backup=$(realpath -e -- "$backup_input")
stamp=$(date -u +%Y%m%dT%H%M%SZ)
token="${stamp,,}-$$"
container="qingkui-recovery-postgres-${token}"
volume="qingkui_recovery_${token//-/_}"
workdir=$(mktemp -d "/tmp/qingkui-recovery-${token}.XXXXXX")
report=${report_input:-"$PWD/recovery-drill-${stamp}.json"}
report_parent=$(dirname -- "$report")
mkdir -p -- "$report_parent"

cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
  rm -rf -- "$workdir"
}
trap cleanup EXIT

echo "[1/5] Verifying gzip archive"
gzip -t -- "$backup"
backup_sha256=$(sha256sum -- "$backup" | awk '{print $1}')

echo "[2/5] Starting isolated PostgreSQL"
docker volume create "$volume" >/dev/null
docker run -d --name "$container" \
  -e POSTGRES_DB=qingkui \
  -e POSTGRES_USER=qingkui \
  -e "POSTGRES_PASSWORD=recovery-${token}" \
  -v "$volume:/var/lib/postgresql/data" \
  postgres:17-alpine >/dev/null
for _ in $(seq 1 60); do
  if docker exec "$container" pg_isready -U qingkui -d qingkui >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$container" pg_isready -U qingkui -d qingkui >/dev/null

echo "[3/5] Restoring backup into isolated database"
gzip -dc -- "$backup" | docker exec -i "$container" psql -v ON_ERROR_STOP=1 -U qingkui -d qingkui >/dev/null
db_metrics=$(docker exec "$container" psql -v ON_ERROR_STOP=1 -U qingkui -d qingkui -Atc \
  "select json_build_object(
    'alembic_version', (select version_num from alembic_version limit 1),
    'users', (select count(*) from users),
    'documents', (select count(*) from knowledge_documents),
    'chunks', (select count(*) from knowledge_chunks),
    'nodes', (select count(*) from knowledge_nodes),
    'messages', (select count(*) from messages)
  );")

echo "[4/5] Verifying current and previous OSS vector indexes in temporary storage"
docker compose -p qingkui run --rm \
  -e RETRIEVAL_VECTOR_INDEX_PATH=/recovery/current.npz \
  -v "$workdir:/recovery" \
  vector-sync python -m app.cli sync-vector-index-from-oss --version current >/dev/null
docker compose -p qingkui run --rm \
  -e RETRIEVAL_VECTOR_INDEX_PATH=/recovery/previous.npz \
  -v "$workdir:/recovery" \
  vector-sync python -m app.cli sync-vector-index-from-oss --version previous >/dev/null
current_sha256=$(sha256sum "$workdir/current.npz" | awk '{print $1}')
previous_sha256=$(sha256sum "$workdir/previous.npz" | awk '{print $1}')

oss_status="index_versions_verified"
if [[ $full_oss -eq 1 ]]; then
  echo "[5/5] Auditing all OSS document objects"
  docker compose -p qingkui run --rm vector-sync \
    python -m app.cli verify-oss-storage --workers 8 >/dev/null
  oss_status="full_audit_verified"
else
  echo "[5/5] Full OSS document audit skipped (use --full-oss to enable)"
fi

python - "$report" "$stamp" "$backup" "$backup_sha256" "$db_metrics" \
  "$current_sha256" "$previous_sha256" "$oss_status" <<'PY'
import json
import sys
from pathlib import Path

output, stamp, backup, backup_sha256, db_metrics, current, previous, oss_status = sys.argv[1:]
report = {
    "schema": "qingkui-recovery-drill-v1",
    "completed_at": stamp,
    "status": "passed",
    "production_data_modified": False,
    "database": {
        "backup": backup,
        "backup_sha256": backup_sha256,
        "isolated_restore": True,
        "metrics": json.loads(db_metrics),
    },
    "vector_indexes": {
        "current_sha256": current,
        "previous_sha256": previous,
    },
    "object_storage": {"status": oss_status},
}
Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
PY

echo "Recovery drill passed; report=$report"
