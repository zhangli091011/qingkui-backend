#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  echo "Usage: $0 [--full-oss] <backup.sql.gz|backup.dump> [report.json]" >&2
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
python_bin=$(command -v python3 || command -v python || true)
if [[ -z "$python_bin" ]]; then
  echo "python3 or python is required to write the recovery report" >&2
  exit 2
fi

backup_input=$1
report_input=${2:-}
if [[ ! -f "$backup_input" || ( "$backup_input" != *.sql.gz && "$backup_input" != *.dump ) ]]; then
  echo "Backup must be an existing .sql.gz or PostgreSQL custom-format .dump file" >&2
  exit 2
fi
backup=$(realpath -e -- "$backup_input")
checksum_input="${backup_input}.sha256"
if [[ ! -f "$checksum_input" ]]; then
  echo "Backup checksum file is required: $checksum_input" >&2
  exit 2
fi
checksum_file=$(realpath -e -- "$checksum_input")
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

echo "[1/5] Verifying backup archive and checksum"
if [[ "$backup" == *.sql.gz ]]; then
  gzip -t -- "$backup"
else
  archive_header=$(head -c 5 -- "$backup")
  if [[ "$archive_header" != "PGDMP" ]]; then
    echo "Custom-format backup does not have a PGDMP header" >&2
    exit 1
  fi
fi
backup_sha256=$(sha256sum -- "$backup" | awk '{print $1}')
expected_backup_sha256=$(awk 'NR == 1 {print tolower($1)}' "$checksum_file")
if [[ ! "$expected_backup_sha256" =~ ^[0-9a-f]{64}$ || "$backup_sha256" != "$expected_backup_sha256" ]]; then
  echo "Backup SHA-256 does not match $checksum_file" >&2
  exit 1
fi

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
if [[ "$backup" == *.sql.gz ]]; then
  gzip -dc -- "$backup" | docker exec -i "$container" psql -v ON_ERROR_STOP=1 -U qingkui -d qingkui >/dev/null
else
  docker exec -i "$container" pg_restore --exit-on-error --no-owner --no-privileges -U qingkui -d qingkui < "$backup"
fi
db_metrics=$(docker exec "$container" psql -v ON_ERROR_STOP=1 -U qingkui -d qingkui -Atc \
  "select json_build_object(
    'alembic_version', (select version_num from alembic_version limit 1),
    'users', (select count(*) from users),
    'documents', (select count(*) from knowledge_documents),
    'chunks', (select count(*) from knowledge_chunks),
    'nodes', (select count(*) from knowledge_nodes),
    'messages', (select count(*) from messages)
  );")
"$python_bin" - "$db_metrics" <<'PY'
import json
import sys

metrics = json.loads(sys.argv[1])
required_nonempty = ("users", "documents", "chunks", "nodes")
missing = [name for name in required_nonempty if int(metrics.get(name) or 0) <= 0]
if not metrics.get("alembic_version"):
    raise SystemExit("Restored database has no Alembic revision")
if missing:
    raise SystemExit("Restored production backup has empty core tables: " + ", ".join(missing))
PY

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

"$python_bin" - "$report" "$stamp" "$backup" "$checksum_file" "$backup_sha256" "$db_metrics" \
  "$current_sha256" "$previous_sha256" "$oss_status" <<'PY'
import json
import sys
from pathlib import Path

output, stamp, backup, checksum_file, backup_sha256, db_metrics, current, previous, oss_status = sys.argv[1:]
report = {
    "schema": "qingkui-recovery-drill-v1",
    "completed_at": stamp,
    "status": "passed",
    "production_data_modified": False,
    "database": {
        "backup": backup,
        "checksum_file": checksum_file,
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
