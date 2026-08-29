# OSS knowledge storage

## Boundaries

- PostgreSQL remains on the API server and is the source of truth for users, documents, chunks, embeddings and graph data.
- The private OSS bucket stores only original PDF/document objects, immutable vector-index versions and migration manifests.
- Android never receives OSS credentials and never reads OSS directly. It uses authenticated backend APIs.
- The request path never downloads the vector index from OSS. The API reads `/data/qingkui-vectors.npz` from a read-only mount.

## Object layout

```text
knowledge/source/sha256/<first-2>/<sha256>/<filename>
knowledge/index/versions/<sha256>.npz
knowledge/index/current.json
knowledge/manifests/migration-<UTC timestamp>.json
```

Source files and indexes are content addressed, so retries are idempotent and newly imported duplicate bytes do not create another object. `current.json` contains the current and previous index descriptors. Every descriptor includes object key, byte size and SHA-256.

## Production flow

1. Build the local sidecar after document embeddings are committed.
2. Run a dry run and inspect the document count and total bytes.
3. Upload sources and the versioned index. The pointer is published only after object verification and the manifest upload succeed.
4. Run the full OSS audit.
5. Run a strict index sync before deployment. The downloaded file is checked before an atomic local replacement.
6. Start the API with the vector data directory mounted read-only.

```bash
docker compose -p qingkui run --rm vector-sync \
  python -m app.cli migrate-to-oss --workers 8 --dry-run
docker compose -p qingkui run --rm vector-sync \
  python -m app.cli migrate-to-oss --workers 8
docker compose -p qingkui run --rm vector-sync \
  python -m app.cli verify-oss-storage --workers 8
docker compose -p qingkui run --rm vector-sync \
  python -m app.cli sync-vector-index-from-oss
docker compose -p qingkui up -d
```

The normal Compose startup uses `--allow-stale`: a temporary OSS outage does not block API restart when a local index already exists. Release validation still uses the strict command above and must fail on an unavailable or corrupt object.

## Rollback

To install the previous published vector version:

```bash
docker compose -p qingkui run --rm vector-sync \
  python -m app.cli sync-vector-index-from-oss --version previous
docker compose -p qingkui up -d --force-recreate api
```

Do not delete the previous OSS version until the replacement has passed retrieval checks and the rollback window has elapsed. PostgreSQL backup/restore remains a separate operation.

## Security and cost

- Keep Block Public Access enabled and grant the server RAM identity access only to this bucket/prefix.
- Store credentials only in the server secret environment; never in Git, APKs, images or manifests.
- Prefer the Qingdao internal endpoint only after confirming that the server is an Alibaba Cloud ECS instance in the same region. Otherwise use the public Qingdao endpoint.
- Keep active indexes in Standard storage. Lifecycle old source documents to IA only when retrieval latency and minimum-storage-duration charges have been accepted.
- Rotate any AccessKey that has appeared in chat, shell history or logs.
