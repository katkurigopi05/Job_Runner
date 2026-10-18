import asyncio
import collections
import hashlib
import json
import os
from pathlib import Path
import statistics
import time

import asyncpg
import httpx
from sqlalchemy.engine import make_url

from packages.core.config import get_settings
from packages.llm import audit


def emit(name, value):
    print(json.dumps({"section": name, "value": value}, default=str), flush=True)


async def main():
    settings = get_settings()
    url = make_url(settings.database_url)
    emit("target", {"host": url.host, "port": url.port, "database": url.database})
    safe_keys = ["auto_submit", "allow_non_local", "embedding_backend", "chat_rerank_model",
                 "chat_semantic_candidates", "ollama_model", "llm_provider", "llm_task_tailor",
                 "notify_backends", "storage_root", "vault_root", "backup_root",
                 "db_pool_size", "db_max_overflow", "posting_max_age_days"]
    emit("settings", {k: getattr(settings, k) for k in safe_keys if hasattr(settings, k)})
    emit("imap_configured", bool(getattr(settings, "imap_host", "")))
    local = Path(".claude/settings.local.json")
    if local.exists():
        obj = json.loads(local.read_text())
        emit("mcp_config", {k: obj.get(k) for k in ("enabledMcpjsonServers", "disabledMcpjsonServers", "enableAllProjectMcpServers")})
    trail = audit.audit_path()
    if trail.exists():
        before = hashlib.sha256(trail.read_bytes()).hexdigest()
        entries = list(audit.read_trail())
        emit("audit", {"entries": len(entries), "uploads_by_task": dict(collections.Counter(e.task for e in entries if e.left_machine)), "providers": dict(collections.Counter(e.provider for e in entries)), "unchanged": before == hashlib.sha256(trail.read_bytes()).hexdigest()})
    conn = await asyncpg.connect(host=url.host or "localhost", port=url.port or 5432,
        user=url.username, password=url.password, database=url.database, timeout=5,
        server_settings={"default_transaction_read_only": "on", "statement_timeout": "15000", "application_name": "jobrunner_readonly_audit"})
    queries = {
        "identity": "SELECT current_database() AS database, current_user AS role, current_setting('transaction_read_only') AS read_only, current_setting('server_version') AS version, pg_database_size(current_database()) AS database_bytes",
        "roles": "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls FROM pg_roles WHERE rolcanlogin",
        "databases": "SELECT datname, shobj_description(oid, 'pg_database') AS marker FROM pg_database WHERE datname LIKE 'jobrunner%'",
        "revision": "SELECT version_num FROM alembic_version",
        "extension": "SELECT extname, extversion FROM pg_extension",
        "counts": "SELECT (SELECT count(*) FROM companies) AS companies, (SELECT count(*) FROM profiles) AS profiles, (SELECT count(*) FROM matches) AS matches, (SELECT count(*) FROM posting_labels) AS owner_labels, (SELECT count(*) FROM inbound_messages) AS mail, (SELECT count(*) FROM resumes) AS resumes",
        "companies": "SELECT source_status, count(*) FROM companies GROUP BY source_status",
        "postings": "SELECT count(*) AS total, count(*) FILTER(WHERE closed_at IS NULL) AS open, count(*) FILTER(WHERE closed_at IS NULL AND description_embedding IS NULL) AS open_missing_vectors, count(*) FILTER(WHERE closed_at IS NULL AND description_raw IS NULL) AS open_no_text, count(*) FILTER(WHERE first_seen_at > now()-interval '24 hours') AS first_seen_24h FROM postings",
        "posting_spaces": "SELECT embedding_model, embedding_revision, count(*) FROM postings WHERE closed_at IS NULL GROUP BY 1,2",
        "chunks": "SELECT c.embedding_model, count(*) AS total, count(*) FILTER(WHERE p.closed_at IS NULL) AS open, count(*) FILTER(WHERE c.content_hash IS DISTINCT FROM p.content_hash) AS stale, max(c.created_at) AS last_created FROM posting_chunks c JOIN postings p ON p.id=c.posting_id GROUP BY 1",
        "chunk_backlog": "SELECT count(*) AS open_with_text_missing_current_bge_chunks FROM postings p WHERE p.closed_at IS NULL AND length(coalesce(p.description_raw,''))>0 AND NOT EXISTS (SELECT 1 FROM posting_chunks c WHERE c.posting_id=p.id AND c.content_hash IS NOT DISTINCT FROM p.content_hash AND c.embedding_model='sentence-transformers')",
        "applications": "SELECT status, count(*) FROM applications GROUP BY status",
        "ledger": "SELECT state, source, count(*) FROM applied_postings GROUP BY state,source",
        "queue": "SELECT kind,status,count(*) AS n, count(*) FILTER(WHERE status='running' AND lease_expires_at<now()) AS expired, min(created_at) FILTER(WHERE status='pending' AND run_after<=now()) AS oldest_due, max(attempts) AS max_attempts FROM queue_tasks GROUP BY kind,status ORDER BY kind,status",
        "workers": "SELECT count(*) AS total, count(*) FILTER(WHERE stopped_at IS NULL AND last_seen_at > now()-interval '90 seconds') AS alive, max(last_seen_at) AS latest_beat FROM worker_heartbeats",
        "crawls": "SELECT status,count(*) AS n,max(started_at) AS latest_start,max(finished_at) AS latest_finish FROM crawl_runs GROUP BY status",
        "board_failures": "SELECT count(*) AS boards_with_failures, sum((SELECT count(*) FROM postings p WHERE p.company_id=s.company_id AND p.closed_at IS NULL)) AS open_postings, min(last_success_at) AS oldest_success FROM company_crawl_states s WHERE consecutive_failures>0",
        "sizes": "SELECT relname, pg_total_relation_size(relid) AS total_bytes,pg_indexes_size(relid) AS index_bytes FROM pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 8",
        "vector_indexes": "SELECT indexname,indexdef FROM pg_indexes WHERE tablename IN ('postings','posting_chunks')",
        "connections": "SELECT datname, state, count(*) AS n FROM pg_stat_activity WHERE datname LIKE 'jobrunner%' GROUP BY datname,state",
    }
    try:
        async with conn.transaction(readonly=True):
            for name, sql in queries.items():
                try:
                    async with conn.transaction():
                        emit(name, [dict(r) for r in await conn.fetch(sql)])
                except Exception as exc:
                    emit(name, {"error_type": type(exc).__name__})
            query = "SELECT c.id FROM posting_chunks c JOIN postings p ON p.id=c.posting_id WHERE p.closed_at IS NULL AND c.content_hash IS NOT DISTINCT FROM p.content_hash AND c.embedding_model='sentence-transformers' ORDER BY c.embedding <=> (SELECT embedding FROM posting_chunks WHERE embedding_model='sentence-transformers' LIMIT 1) LIMIT 200"
            times = []
            for _ in range(9):
                start = time.perf_counter()
                await conn.fetch(query)
                times.append(round((time.perf_counter()-start)*1000, 2))
            emit("exact_chunk_scan_ms", {"samples": times, "median": statistics.median(times), "max": max(times), "query_vector": "one stored vector; no model loaded"})
            emit("query_plan", [r[0] for r in await conn.fetch("EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) " + query)])
    finally:
        await conn.close()
    async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
        for port in (8000, 3001):
            for host in ("localhost", "evil.example"):
                path = "/health" if port==8000 else "/api/health"
                try:
                    response = await client.get(f"http://127.0.0.1:{port}{path}", headers={"Host": host})
                    emit("http_probe", {"port": port, "host": host, "status": response.status_code})
                except Exception as exc:
                    emit("http_probe", {"port": port, "host": host, "error_type": type(exc).__name__})


asyncio.run(main())
