from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sqlite3
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ctfka.contracts import DiscoveredItem, FetchedDocument, ParsedDocument
from ctfka.indexing.storage import IndexStorageMixin
from ctfka.ingestion.parser import parse_document

_SCHEMA_VERSION = 2
_SYMBOL_RE = re.compile(r"(?<![\w])(?:[A-Za-z_][A-Za-z0-9_.$@:+/-]*|CVE-\d{4}-\d+)(?![\w])")
_CJK_RE = re.compile(r"[\u3400-\u9fff]+")


def _now() -> float:
    return time.time()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return url.strip()
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            re.sub(r"/{2,}", "/", parts.path or "/"),
            parts.query,
            "",
        )
    )


def _search_terms(text: str) -> list[str]:
    terms = set(_SYMBOL_RE.findall(text))
    for run in _CJK_RE.findall(text):
        terms.update(run[i : i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            terms.add(run)
    terms.update(re.findall(r"[A-Za-z0-9]+", text))
    return sorted(t for t in terms if t)


def _simhash(text: str) -> int:
    tokens = set(re.findall(r"[\w\u3400-\u9fff]+", text.lower()))
    scores = [0] * 64
    for token in tokens:
        h = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            scores[bit] += 1 if h >> bit & 1 else -1
    value = sum((1 << i) for i, score in enumerate(scores) if score >= 0)
    return value if value < (1 << 63) else value - (1 << 64)


class Store(IndexStorageMixin):
    """SQLite-backed article, job, state and immutable raw snapshot store."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.db_dir = self.data_dir / "db"
        self.raw_dir = self.data_dir / "raw"
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.db_dir / "ctfka.sqlite3"
        self._write_lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @contextmanager
    def _connection(self):
        db = self._connect()
        try:
            yield db
        finally:
            db.close()

    def _initialize(self) -> None:
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
            )
            version = db.execute(
                "SELECT COALESCE(MAX(version),0) FROM schema_migrations"
            ).fetchone()[0]
            if version < 1:
                db.executescript("""
                CREATE TABLE sources(name TEXT PRIMARY KEY, adapter TEXT NOT NULL, config_json TEXT NOT NULL DEFAULT '{}', state_json TEXT NOT NULL DEFAULT '{}', updated_at REAL NOT NULL);
                CREATE TABLE articles(id TEXT PRIMARY KEY, source TEXT NOT NULL REFERENCES sources(name), identity TEXT NOT NULL, url TEXT NOT NULL, canonical_url TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL, content_sha256 TEXT NOT NULL, raw_sha256 TEXT NOT NULL, raw_path TEXT NOT NULL, version INTEGER NOT NULL, parser_version TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', archived INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(source,identity));
                CREATE INDEX articles_sha_idx ON articles(content_sha256); CREATE INDEX articles_url_idx ON articles(canonical_url);
                CREATE TABLE article_versions(id INTEGER PRIMARY KEY AUTOINCREMENT, article_id TEXT NOT NULL REFERENCES articles(id), version INTEGER NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL, content_sha256 TEXT NOT NULL, raw_sha256 TEXT NOT NULL, raw_path TEXT NOT NULL, metadata_json TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(article_id,version));
                CREATE TABLE crawl_jobs(id TEXT PRIMARY KEY, source TEXT NOT NULL REFERENCES sources(name), url TEXT NOT NULL, identity TEXT NOT NULL, metadata_json TEXT NOT NULL, status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 5, available_at REAL NOT NULL, lease_until REAL, lease_token TEXT, error TEXT, error_category TEXT, result_json TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE UNIQUE INDEX active_job_identity ON crawl_jobs(source,identity) WHERE status IN ('pending','running','retry_waiting'); CREATE INDEX jobs_ready_idx ON crawl_jobs(status,available_at,priority,created_at);
                CREATE TABLE source_state(source TEXT PRIMARY KEY REFERENCES sources(name), state_json TEXT NOT NULL, updated_at REAL NOT NULL);
                """)
                try:
                    db.execute(
                        "CREATE VIRTUAL TABLE article_fts USING fts5(article_id UNINDEXED, title, content, symbols, tokenize='unicode61 remove_diacritics 0')"
                    )
                except sqlite3.OperationalError as exc:
                    raise RuntimeError("SQLite was built without FTS5 support") from exc
                db.execute("INSERT INTO schema_migrations VALUES(1,?)", (_now(),))
                version = 1
            columns = {row["name"] for row in db.execute("PRAGMA table_info(articles)").fetchall()}
            if version < 2 or "simhash" not in columns:
                db.execute("ALTER TABLE articles ADD COLUMN simhash INTEGER")
                db.execute("CREATE INDEX articles_simhash_idx ON articles(simhash)")
                for row in db.execute(
                    "SELECT id,content FROM articles WHERE simhash IS NULL"
                ).fetchall():
                    db.execute(
                        "UPDATE articles SET simhash=? WHERE id=?",
                        (_simhash(row["content"]), row["id"]),
                    )
                db.execute("INSERT OR REPLACE INTO schema_migrations VALUES(2,?)", (_now(),))
        self._initialize_stage2()

    def register_source(self, name: str, adapter: str, config: dict | None = None) -> None:
        now = _now()
        with self._write_lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO sources(name,adapter,config_json,updated_at) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET adapter=excluded.adapter,config_json=excluded.config_json,updated_at=excluded.updated_at",
                (name, adapter, _json(config or {}), now),
            )
            db.execute(
                "INSERT OR IGNORE INTO source_state(source,state_json,updated_at) VALUES(?,?,?)",
                (name, "{}", now),
            )
            db.commit()

    def _write_raw(self, data: bytes) -> tuple[str, str]:
        digest = hashlib.sha256(data).hexdigest()
        path = self.raw_dir / digest[:2] / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise RuntimeError(f"immutable raw snapshot is corrupt: {path}")
        else:
            temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
            with temp.open("xb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(temp, path)
            except FileExistsError:
                pass
            except OSError:
                if not path.exists():
                    os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)
        return digest, str(path)

    def ingest(
        self,
        source: str,
        document: FetchedDocument,
        parsed: ParsedDocument | None = None,
        *,
        job_id: str | None = None,
        lease_token: str | None = None,
    ) -> dict:
        parsed = parsed or parse_document(document)
        content_hash = hashlib.sha256(parsed.content.encode()).hexdigest()
        canonical = _canonical_url(document.url)
        identity = str(document.metadata.get("identity") or canonical)
        symbols = " ".join(_search_terms(parsed.title + "\n" + parsed.content))
        with self._write_lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _now()
            if job_id is not None:
                lease = db.execute(
                    "SELECT source,status,lease_token,lease_until FROM crawl_jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                if (
                    not lease_token
                    or lease is None
                    or lease["source"] != source
                    or lease["status"] != "running"
                    or lease["lease_token"] != lease_token
                    or lease["lease_until"] is None
                    or lease["lease_until"] <= now
                ):
                    db.rollback()
                    raise ValueError("job lease lost")
            raw_hash, raw_path = self._write_raw(document.raw)
            if not db.execute("SELECT 1 FROM sources WHERE name=?", (source,)).fetchone():
                db.execute(
                    "INSERT INTO sources(name,adapter,updated_at) VALUES(?,?,?)",
                    (source, "unknown", now),
                )
            existing = db.execute(
                "SELECT * FROM articles WHERE source=? AND identity=?", (source, identity)
            ).fetchone()
            if existing is None:
                article_id = uuid.uuid4().hex
                version = 1
                changed = True
                db.execute(
                    "INSERT INTO articles(id,source,identity,url,canonical_url,title,content,content_sha256,raw_sha256,raw_path,version,parser_version,metadata_json,simhash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        article_id,
                        source,
                        identity,
                        document.url,
                        canonical,
                        parsed.title,
                        parsed.content,
                        content_hash,
                        raw_hash,
                        raw_path,
                        version,
                        str(parsed.metadata.get("parser_version", "1")),
                        _json({**document.metadata, **parsed.metadata}),
                        _simhash(parsed.content),
                        now,
                        now,
                    ),
                )
                db.execute(
                    "INSERT INTO article_versions(article_id,version,title,content,content_sha256,raw_sha256,raw_path,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        article_id,
                        version,
                        parsed.title,
                        parsed.content,
                        content_hash,
                        raw_hash,
                        raw_path,
                        _json({**document.metadata, **parsed.metadata}),
                        now,
                    ),
                )
            elif (
                existing["content_sha256"] == content_hash
                and existing["raw_sha256"] == raw_hash
                and existing["title"] == parsed.title
                and existing["parser_version"] == str(parsed.metadata.get("parser_version", "1"))
            ):
                article_id, version, changed = existing["id"], existing["version"], False
                db.execute(
                    "UPDATE articles SET url=?,canonical_url=?,metadata_json=?,archived=0,updated_at=? WHERE id=?",
                    (
                        document.url,
                        canonical,
                        _json({**document.metadata, **parsed.metadata}),
                        now,
                        article_id,
                    ),
                )
            else:
                article_id, version, changed = existing["id"], existing["version"] + 1, True
                db.execute(
                    "INSERT INTO article_versions(article_id,version,title,content,content_sha256,raw_sha256,raw_path,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        article_id,
                        version,
                        parsed.title,
                        parsed.content,
                        content_hash,
                        raw_hash,
                        raw_path,
                        _json({**document.metadata, **parsed.metadata}),
                        now,
                    ),
                )
                db.execute(
                    "UPDATE articles SET url=?,canonical_url=?,title=?,content=?,content_sha256=?,raw_sha256=?,raw_path=?,version=?,parser_version=?,metadata_json=?,simhash=?,archived=0,updated_at=? WHERE id=?",
                    (
                        document.url,
                        canonical,
                        parsed.title,
                        parsed.content,
                        content_hash,
                        raw_hash,
                        raw_path,
                        version,
                        str(parsed.metadata.get("parser_version", "1")),
                        _json({**document.metadata, **parsed.metadata}),
                        _simhash(parsed.content),
                        now,
                        article_id,
                    ),
                )
            duplicate = db.execute(
                "SELECT id,source,identity,title,url FROM articles WHERE content_sha256=? AND id<>?",
                (content_hash, article_id),
            ).fetchall()
            if changed:
                db.execute("DELETE FROM article_fts WHERE article_id=?", (article_id,))
                db.execute(
                    "INSERT INTO article_fts(article_id,title,content,symbols) VALUES(?,?,?,?)",
                    (article_id, parsed.title, parsed.content, symbols),
                )
            near = self._near_duplicates(db, parsed.content, article_id)
            db.commit()
        return {
            "id": article_id,
            "version": version,
            "raw_path": raw_path,
            "duplicate": (not changed) or bool(duplicate),
            "exact_duplicates": [dict(r) for r in duplicate],
            "near_duplicates": near,
            "content_sha256": content_hash,
            "raw_sha256": raw_hash,
            "changed": changed,
        }

    @staticmethod
    def _near_duplicates(db: sqlite3.Connection, content: str, article_id: str) -> list[dict]:
        signature = _simhash(content) & ((1 << 64) - 1)
        rows = db.execute(
            "SELECT id,title,simhash,content_sha256 FROM articles WHERE id<>? AND simhash IS NOT NULL ORDER BY updated_at DESC LIMIT 2000",
            (article_id,),
        ).fetchall()
        found = []
        for row in rows:
            if (signature ^ (int(row["simhash"]) & ((1 << 64) - 1))).bit_count() <= 8:
                found.append(
                    {
                        "id": row["id"],
                        "title": row["title"],
                        "simhash_distance": (
                            signature ^ (int(row["simhash"]) & ((1 << 64) - 1))
                        ).bit_count(),
                        "content_sha256": row["content_sha256"],
                    }
                )
                if len(found) >= 10:
                    break
        return found

    def search(self, query: str, limit: int = 20, source: str | None = None) -> list[dict]:
        if limit < 1 or not query.strip():
            return []
        query = query.strip()
        terms = _search_terms(query)
        results = {}
        with self._connection() as db:
            fts_terms = [t for t in terms if re.fullmatch(r"[\w\u3400-\u9fff]+", t)]
            if fts_terms:
                fts_query = " OR ".join('"' + t.replace('"', '""') + '"' for t in fts_terms)
                sql = "SELECT a.*,snippet(article_fts,2,'[',']','…',12) snippet,bm25(article_fts) rank FROM article_fts JOIN articles a ON a.id=article_fts.article_id WHERE article_fts MATCH ? AND a.archived=0"
                args: list[Any] = [fts_query]
                if source:
                    sql += " AND a.source=?"
                    args.append(source)
                sql += " ORDER BY rank LIMIT ?"
                args.append(max(limit * 4, 50))
                for row in db.execute(sql, args):
                    data = self._article_dict(row)
                    data.update(
                        snippet=row["snippet"],
                        score=-float(row["rank"]),
                        evidence_offsets=self._evidence_offsets(row["content"], query),
                        heading=self._heading_at(
                            row["content"], row["content"].casefold().find(query.casefold())
                        ),
                    )
                    results[row["id"]] = data
            sql = "SELECT * FROM articles WHERE archived=0 AND (instr(lower(title),lower(?))>0 OR instr(lower(content),lower(?))>0)"
            args: list[Any] = [query, query]
            if source:
                sql += " AND source=?"
                args.append(source)
            sql += " ORDER BY CASE WHEN instr(lower(title),lower(?))>0 THEN 0 WHEN instr(lower(content),lower(?))>0 THEN 1 ELSE 2 END, updated_at DESC LIMIT ?"
            args.extend([query, query, max(limit * 4, 50)])
            for row in db.execute(sql, args):
                content = row["content"]
                title = row["title"]
                in_title = query.casefold() in title.casefold()
                pos = (
                    title.casefold().find(query.casefold())
                    if in_title
                    else content.casefold().find(query.casefold())
                )
                excerpt = title if in_title else content
                snippet = excerpt[
                    max(0, pos - 140) : min(len(excerpt), pos + len(query) + 180)
                ].strip()
                data = results.get(row["id"], self._article_dict(row))
                data.update(
                    snippet=data.get("snippet") or snippet,
                    evidence_offsets=self._evidence_offsets(content, query),
                    heading=self._heading_at(content, pos) if not in_title else title,
                    score=max(float(data.get("score", 0)), 100),
                )
                results[row["id"]] = data
        return sorted(results.values(), key=lambda x: (-x.get("score", 0), x["title"].casefold()))[
            :limit
        ]

    @staticmethod
    def _evidence_offsets(content: str, query: str) -> list[dict]:
        offsets = []
        start = 0
        hay = content.casefold()
        needle = query.casefold()
        if not needle:
            return offsets
        while (pos := hay.find(needle, start)) >= 0 and len(offsets) < 20:
            offsets.append({"start": pos, "end": pos + len(query)})
            start = pos + max(1, len(query))
        return offsets

    @staticmethod
    def _heading_at(content: str, offset: int) -> str | None:
        heading = None
        for match in re.finditer(r"(?m)^#{1,6}\s+(.+)$", content):
            if match.start() > offset:
                break
            heading = match.group(1).strip()
        return heading

    @staticmethod
    def _article_dict(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["metadata"] = json.loads(data.pop("metadata_json", "{}") or "{}")
        data["archived"] = bool(data.get("archived", 0))
        data["quality_status"] = "source_claimed"
        data["verification_status"] = "unverified"
        return data

    def list_articles(self, limit: int = 50, offset: int = 0) -> list[dict]:
        with self._connection() as db:
            return [
                self._article_dict(r)
                for r in db.execute(
                    "SELECT * FROM articles ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            ]

    def get_article(self, article_id: str) -> dict | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
            if not row:
                return None
            result = self._article_dict(row)
            result["versions"] = [
                dict(v)
                for v in db.execute(
                    "SELECT version,title,content_sha256,raw_sha256,raw_path,metadata_json,created_at FROM article_versions WHERE article_id=? ORDER BY version",
                    (article_id,),
                )
            ]
            for v in result["versions"]:
                v["metadata"] = json.loads(v.pop("metadata_json"))
            return result

    def stats(self) -> dict:
        with self._connection() as db:
            return {
                "sources": db.execute("SELECT count(*) FROM sources").fetchone()[0],
                "articles": db.execute("SELECT count(*) FROM articles WHERE archived=0").fetchone()[
                    0
                ],
                "archived": db.execute("SELECT count(*) FROM articles WHERE archived=1").fetchone()[
                    0
                ],
                "versions": db.execute("SELECT count(*) FROM article_versions").fetchone()[0],
                "raw_bytes": sum(
                    p.stat().st_size
                    for p in self.raw_dir.rglob("*")
                    if p.is_file() and not p.name.endswith(".tmp")
                ),
                "schema_version": _SCHEMA_VERSION,
                "stage2_schema_version": db.execute("SELECT COALESCE(MAX(version),0) FROM stage2_schema_migrations").fetchone()[0],
            }

    def rebuild_index(self) -> dict:
        with self._write_lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM article_fts")
            rows = db.execute("SELECT id,title,content FROM articles WHERE archived=0").fetchall()
            db.executemany(
                "INSERT INTO article_fts(article_id,title,content,symbols) VALUES(?,?,?,?)",
                [
                    (
                        r["id"],
                        r["title"],
                        r["content"],
                        " ".join(_search_terms(r["title"] + "\n" + r["content"])),
                    )
                    for r in rows
                ],
            )
            db.commit()
        return {"indexed": len(rows)}

    def enqueue_job(self, source: str, item: DiscoveredItem, priority: int = 0) -> dict:
        now = _now()
        ident = item.identity or _canonical_url(item.url)
        with self._write_lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO sources(name,adapter,updated_at) VALUES(?,?,?)",
                (source, "unknown", now),
            )
            row = db.execute(
                "SELECT * FROM crawl_jobs WHERE source=? AND identity=? AND status IN ('pending','running','retry_waiting')",
                (source, ident),
            ).fetchone()
            if row:
                db.execute(
                    "UPDATE crawl_jobs SET url=?,metadata_json=?,priority=max(priority,?),updated_at=? WHERE id=?",
                    (item.url, _json(item.metadata), priority, now, row["id"]),
                )
                job_id, created = row["id"], False
            else:
                job_id = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO crawl_jobs(id,source,url,identity,metadata_json,status,priority,available_at,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?,?,?)",
                    (
                        job_id,
                        source,
                        item.url,
                        ident,
                        _json(item.metadata),
                        priority,
                        now,
                        now,
                        now,
                    ),
                )
                created = True
            db.commit()
        return {
            "id": job_id,
            "source": source,
            "url": item.url,
            "identity": ident,
            "created": created,
        }

    def claim_job(self, source: str | None = None, lease_seconds: float = 300) -> dict | None:
        now = _now()
        with self._write_lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE crawl_jobs SET status='pending',lease_until=NULL,lease_token=NULL,updated_at=? WHERE status='running' AND lease_until<=?",
                (now, now),
            )
            sql = "SELECT * FROM crawl_jobs WHERE status IN ('pending','retry_waiting') AND available_at<=?"
            args: list[Any] = [now]
            if source:
                sql += " AND source=?"
                args.append(source)
            row = db.execute(
                sql + " ORDER BY priority DESC,available_at,created_at LIMIT 1", args
            ).fetchone()
            if not row:
                db.commit()
                return None
            db.execute(
                "UPDATE crawl_jobs SET status='running',attempts=attempts+1,lease_until=?,lease_token=?,updated_at=? WHERE id=?",
                (now + lease_seconds, uuid.uuid4().hex, now, row["id"]),
            )
            updated = db.execute("SELECT * FROM crawl_jobs WHERE id=?", (row["id"],)).fetchone()
            db.commit()
        return self._job_dict(updated)

    @staticmethod
    def _job_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["metadata"] = json.loads(d.pop("metadata_json"))
        r = d.pop("result_json")
        d["result"] = json.loads(r) if r else None
        return d

    def heartbeat_job(
        self, job_id: str, lease_seconds: float = 300, lease_token: str | None = None
    ) -> bool:
        with self._write_lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _now()
            sql = "UPDATE crawl_jobs SET lease_until=?,updated_at=? WHERE id=? AND status='running'"
            args: list[Any] = [now + lease_seconds, now, job_id]
            if lease_token is not None:
                sql += " AND lease_token=? AND lease_until>?"
                args.extend([lease_token, now])
            cur = db.execute(sql, args)
            db.commit()
            return cur.rowcount > 0

    def release_job(self, job_id: str, lease_token: str | None = None) -> bool:
        with self._write_lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _now()
            sql = "UPDATE crawl_jobs SET status='pending',lease_until=NULL,lease_token=NULL,updated_at=? WHERE id=? AND status='running'"
            args: list[Any] = [now, job_id]
            if lease_token is not None:
                sql += " AND lease_token=? AND lease_until>?"
                args.extend([lease_token, now])
            cur = db.execute(sql, args)
            db.commit()
            return cur.rowcount > 0

    def complete_job(
        self, job_id: str, result: dict | None = None, lease_token: str | None = None
    ) -> None:
        with self._write_lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _now()
            sql = "UPDATE crawl_jobs SET status='completed',result_json=?,lease_until=NULL,lease_token=NULL,error=NULL,error_category=NULL,updated_at=? WHERE id=? AND status='running'"
            args: list[Any] = [_json(result) if result is not None else None, now, job_id]
            if lease_token is not None:
                sql += " AND lease_token=? AND lease_until>?"
                args.extend([lease_token, now])
            cur = db.execute(sql, args)
            if cur.rowcount == 0:
                row = db.execute("SELECT status FROM crawl_jobs WHERE id=?", (job_id,)).fetchone()
                if lease_token is None and row and row["status"] == "completed":
                    db.rollback()
                    return
                db.rollback()
                if lease_token is not None:
                    raise ValueError("job lease lost")
                raise ValueError(f"job {job_id!r} is not running")
            db.commit()

    def fail_job(
        self,
        job_id: str,
        error: str,
        category: str,
        retry_after: float | None = None,
        max_attempts: int = 5,
        lease_token: str | None = None,
    ) -> None:
        with self._write_lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _now()
            row = db.execute(
                "SELECT attempts,max_attempts,lease_token,lease_until FROM crawl_jobs WHERE id=? AND status='running'",
                (job_id,),
            ).fetchone()
            if (
                not row
                or (lease_token is not None and row["lease_token"] != lease_token)
                or (
                    lease_token is not None
                    and (row["lease_until"] is None or row["lease_until"] <= now)
                )
            ):
                db.rollback()
                if lease_token is not None:
                    raise ValueError("job lease lost")
                raise ValueError(f"job {job_id!r} is not running")
            maximum = max_attempts or row["max_attempts"]
            if category in {"blocked", "permanent", "invalid"}:
                status = "blocked" if category == "blocked" else "failed"
                available = now
            elif row["attempts"] >= maximum:
                status = "failed"
                available = now
            else:
                status = "retry_waiting"
                base = min(3600, 2 ** min(row["attempts"], 12))
                available = now + (
                    max(0, retry_after)
                    if retry_after is not None
                    else base * random.uniform(0.8, 1.2)
                )
            sql = "UPDATE crawl_jobs SET status=?,error=?,error_category=?,available_at=?,lease_until=NULL,lease_token=NULL,max_attempts=?,updated_at=? WHERE id=? AND status='running'"
            args: list[Any] = [status, error[:4000], category, available, maximum, now, job_id]
            if lease_token is not None:
                sql += " AND lease_token=? AND lease_until>?"
                args.extend([lease_token, now])
            db.execute(sql, args)
            db.commit()

    def retry_job(self, job_id: str) -> bool:
        with self._write_lock, self._connection() as db:
            cur = db.execute(
                "UPDATE crawl_jobs SET status='pending',attempts=0,available_at=?,lease_until=NULL,lease_token=NULL,error=NULL,error_category=NULL,updated_at=? WHERE id=? AND status IN ('failed','blocked','retry_waiting')",
                (_now(), _now(), job_id),
            )
            return cur.rowcount > 0

    def list_jobs(self, limit: int = 100, status: str | None = None) -> list[dict]:
        with self._connection() as db:
            rows = (
                db.execute(
                    "SELECT * FROM crawl_jobs WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                )
                if status
                else db.execute(
                    "SELECT * FROM crawl_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
                )
            )
            return [self._job_dict(r) for r in rows]

    def job_stats(self) -> dict:
        with self._connection() as db:
            return {
                r["status"]: r["count"]
                for r in db.execute("SELECT status,count(*) count FROM crawl_jobs GROUP BY status")
            }

    def recover_jobs(self) -> int:
        now = _now()
        with self._write_lock, self._connection() as db:
            cur = db.execute(
                "UPDATE crawl_jobs SET status='pending',lease_until=NULL,lease_token=NULL,updated_at=? WHERE status='running' AND lease_until<=?",
                (now, now),
            )
            return cur.rowcount

    def next_retry_delay(self) -> float | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT min(available_at) t FROM crawl_jobs WHERE status IN ('pending','retry_waiting')"
            ).fetchone()
            return None if row["t"] is None else max(0.0, row["t"] - _now())

    def get_source_state(self, source: str) -> dict:
        with self._connection() as db:
            row = db.execute(
                "SELECT state_json FROM source_state WHERE source=?", (source,)
            ).fetchone()
            return json.loads(row["state_json"]) if row else {}

    def set_source_state(self, source: str, state: dict) -> None:
        now = _now()
        with self._write_lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO sources(name,adapter,updated_at) VALUES(?,?,?)",
                (source, "unknown", now),
            )
            db.execute(
                "INSERT INTO source_state(source,state_json,updated_at) VALUES(?,?,?) ON CONFLICT(source) DO UPDATE SET state_json=excluded.state_json,updated_at=excluded.updated_at",
                (source, _json(state), now),
            )
            db.execute(
                "UPDATE sources SET state_json=?,updated_at=? WHERE name=?",
                (_json(state), now, source),
            )
            db.commit()

    def archive_missing(self, source: str, identities: list[str]) -> int:
        with self._write_lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TEMP TABLE IF NOT EXISTS scan_identities(identity TEXT PRIMARY KEY)")
            db.execute("DELETE FROM scan_identities")
            db.executemany(
                "INSERT OR IGNORE INTO scan_identities(identity) VALUES(?)",
                ((identity,) for identity in identities),
            )
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM articles WHERE source=? AND archived=0 AND NOT EXISTS (SELECT 1 FROM scan_identities s WHERE s.identity=articles.identity)",
                    (source,),
                )
            ]
            db.executemany(
                "UPDATE articles SET archived=1,updated_at=? WHERE id=?", [(_now(), i) for i in ids]
            )
            db.executemany("DELETE FROM article_fts WHERE article_id=?", [(i,) for i in ids])
            db.commit()
            return len(ids)

    def backup(self, destination: str | Path | None = None) -> str:
        dest = (
            Path(destination)
            if destination
            else self.data_dir / f"backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        )
        dest = dest.expanduser().resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp_db = dest.with_suffix(dest.suffix + ".sqlite.tmp")
        source = self._connect()
        target = sqlite3.connect(temp_db)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        files = {"db/ctfka.sqlite3": hashlib.sha256(temp_db.read_bytes()).hexdigest()}
        for p in self.raw_dir.rglob("*"):
            if p.is_file() and not p.name.endswith(".tmp"):
                files[p.relative_to(self.data_dir).as_posix()] = hashlib.sha256(
                    p.read_bytes()
                ).hexdigest()
        manifest = {"format": "ctfka-backup-v1", "created_at": _now(), "files": files}
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(temp_db, "db/ctfka.sqlite3")
            for p in self.raw_dir.rglob("*"):
                if p.is_file() and not p.name.endswith(".tmp"):
                    z.write(p, p.relative_to(self.data_dir).as_posix())
            z.writestr("manifest.json", _json(manifest))
        temp_db.unlink(missing_ok=True)
        return str(dest)

    def restore(self, archive: str | Path) -> dict:
        with zipfile.ZipFile(archive) as z:
            names = z.namelist()
            if "manifest.json" not in names:
                raise ValueError("backup manifest is missing")
            manifest = json.loads(z.read("manifest.json"))
            if manifest.get("format") != "ctfka-backup-v1":
                raise ValueError("unsupported backup format")
            expected = manifest.get("files", {})
            if set(expected) - set(names):
                raise ValueError("backup is missing manifest files")
            if set(names) - {"manifest.json"} != set(expected):
                raise ValueError("backup contains files absent from manifest")
            for name, digest in expected.items():
                safe = PurePosixPath(name)
                if safe.is_absolute() or ".." in safe.parts or "\\" in name:
                    raise ValueError("unsafe archive path")
                if hashlib.sha256(z.read(name)).hexdigest() != digest:
                    raise ValueError(f"backup integrity check failed: {name}")
            if "db/ctfka.sqlite3" not in expected:
                raise ValueError("backup database missing")
            root = self.data_dir.resolve()
            for name in expected:
                target = (root / Path(*PurePosixPath(name).parts)).resolve()
                if root not in target.parents:
                    raise ValueError("unsafe archive path")
            db_target = root / "db" / "ctfka.sqlite3"
            if db_target.exists():
                current = sqlite3.connect(db_target)
                try:
                    current.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    current.execute("PRAGMA journal_mode=DELETE")
                finally:
                    current.close()
                Path(str(db_target) + "-wal").unlink(missing_ok=True)
                Path(str(db_target) + "-shm").unlink(missing_ok=True)
            for name in expected:
                target = root / Path(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_name(target.name + ".restore.tmp")
                temp.write_bytes(z.read(name))
                os.replace(temp, target)
        self._initialize()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE articles SET raw_path=? || substr(raw_sha256,1,2) || '/' || raw_sha256",
                (str(self.raw_dir) + os.sep,),
            )
            db.execute(
                "UPDATE article_versions SET raw_path=? || substr(raw_sha256,1,2) || '/' || raw_sha256",
                (str(self.raw_dir) + os.sep,),
            )
            db.commit()
            count = db.execute("SELECT count(*) FROM articles").fetchone()[0]
        return {"restored_files": len(expected), "articles": count, "data_dir": str(self.data_dir)}

    def list_sources(self) -> list[dict]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT name,adapter,config_json,state_json,updated_at FROM sources ORDER BY name"
            ).fetchall()
        return [
            {
                "name": r["name"],
                "adapter": r["adapter"],
                "config": json.loads(r["config_json"] or "{}"),
                "state": json.loads(r["state_json"] or "{}"),
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    def job_statuses(self, job_ids: list[str]) -> dict[str, str]:
        if not job_ids:
            return {}
        found: dict[str, str] = {}
        with self._connection() as db:
            for start in range(0, len(job_ids), 800):
                batch = job_ids[start : start + 800]
                placeholders = ",".join("?" for _ in batch)
                found.update(
                    {
                        row["id"]: row["status"]
                        for row in db.execute(
                            f"SELECT id,status FROM crawl_jobs WHERE id IN ({placeholders})", batch
                        )
                    }
                )
        return found
