from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
import time
import uuid
from typing import Any

from ctfka.processing.chunking import chunk_document


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _terms(text: str) -> list[str]:
    words = re.findall(r"[\u3400-\u9fff]+|[A-Za-z0-9_.$@:+/-]+", text)
    output: set[str] = set()
    for word in words:
        if re.fullmatch(r"[\u3400-\u9fff]+", word):
            output.update(word[i : i + 2] for i in range(max(1, len(word) - 1)))
            if len(word) == 1:
                output.add(word)
        else:
            output.add(word)
    return sorted(t for t in output if t)


class IndexStorageMixin:
    """Stage-two SQLite storage, kept separate from article/job lifecycle code."""

    def _initialize_stage2(self) -> None:
        with self._connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS stage2_schema_migrations(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
            version = db.execute("SELECT COALESCE(MAX(version),0) FROM stage2_schema_migrations").fetchone()[0]
            if version < 1:
                db.executescript("""
                CREATE TABLE IF NOT EXISTS chunks(
                    id TEXT PRIMARY KEY, article_id TEXT NOT NULL REFERENCES articles(id), version INTEGER NOT NULL,
                    text TEXT NOT NULL, text_hash TEXT NOT NULL, heading_path_json TEXT NOT NULL,
                    start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, ordinal INTEGER NOT NULL,
                    previous_id TEXT, next_id TEXT, config_fingerprint TEXT NOT NULL, created_at REAL NOT NULL,
                    UNIQUE(article_id,version,ordinal)
                );
                CREATE INDEX IF NOT EXISTS chunks_article_version_idx ON chunks(article_id,version);
                CREATE INDEX IF NOT EXISTS chunks_hash_idx ON chunks(text_hash);
                CREATE TABLE IF NOT EXISTS embedding_manifests(
                    model_key TEXT PRIMARY KEY, model TEXT NOT NULL, digest TEXT NOT NULL,
                    dimension INTEGER NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS embeddings(
                    model_key TEXT NOT NULL REFERENCES embedding_manifests(model_key),
                    text_hash TEXT NOT NULL, vector_json TEXT NOT NULL, created_at REAL NOT NULL,
                    PRIMARY KEY(model_key,text_hash)
                );
                CREATE TABLE IF NOT EXISTS extractions(
                    id TEXT PRIMARY KEY, article_id TEXT NOT NULL REFERENCES articles(id), version INTEGER NOT NULL,
                    model TEXT NOT NULL, model_digest TEXT NOT NULL, status TEXT NOT NULL,
                    payload_json TEXT NOT NULL, error TEXT, created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS extractions_article_idx ON extractions(article_id,created_at);
                CREATE TABLE IF NOT EXISTS knowledge_cards(
                    id TEXT PRIMARY KEY, extraction_id TEXT NOT NULL REFERENCES extractions(id),
                    article_id TEXT NOT NULL REFERENCES articles(id), version INTEGER NOT NULL,
                    title TEXT NOT NULL, content TEXT NOT NULL, evidence_json TEXT NOT NULL,
                    confidence REAL, verification_status TEXT NOT NULL DEFAULT 'unverified', created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS knowledge_article_idx ON knowledge_cards(article_id,version);
            """)
                db.execute("INSERT OR IGNORE INTO stage2_schema_migrations(version,applied_at) VALUES(1,?)", (time.time(),))
                version = 1
            if version < 2:
                db.execute("DROP TABLE IF EXISTS chunk_fts")
                db.execute("CREATE VIRTUAL TABLE chunk_fts USING fts5(chunk_id UNINDEXED,text,symbols,tokenize='unicode61 remove_diacritics 0')")
                chunks = db.execute("SELECT id,text FROM chunks").fetchall()
                db.executemany(
                    "INSERT INTO chunk_fts(chunk_id,text,symbols) VALUES(?,?,?)",
                    [(row["id"], row["text"], " ".join(_terms(row["text"]))) for row in chunks],
                )
                db.execute("INSERT OR REPLACE INTO stage2_schema_migrations(version,applied_at) VALUES(2,?)", (time.time(),))

    @staticmethod
    def _chunk_enriched(db, row) -> dict:
        article = db.execute("SELECT * FROM articles WHERE id=?", (row["article_id"],)).fetchone()
        version = db.execute(
            "SELECT * FROM article_versions WHERE article_id=? AND version=?",
            (row["article_id"], row["version"]),
        ).fetchone()
        result = dict(row)
        result["heading_path"] = json.loads(result.pop("heading_path_json"))
        result["start"] = result.pop("start_offset")
        result["end"] = result.pop("end_offset")
        if article:
            result.update({"title": article["title"], "url": article["url"], "source": article["source"], "archived": bool(article["archived"]), "current": article["version"] == row["version"]})
        if version:
            result.update({"raw_path": version["raw_path"], "version_title": version["title"], "content_sha256": version["content_sha256"]})
        return result

    def sync_chunks(self, article_id: str | None = None, max_chars: int = 1800) -> dict:
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        fingerprint = hashlib.sha256(f"markdown-it:block:v1:{max_chars}".encode()).hexdigest()
        created = updated = 0
        with self._write_lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            skipped_sql = "SELECT COALESCE(SUM((SELECT count(*) FROM chunks c WHERE c.article_id=a.id AND c.version=a.version AND c.config_fingerprint=?)),0) FROM articles a WHERE a.archived=0"
            skipped_params: list[Any] = [fingerprint]
            sql = "SELECT a.id,a.version,a.content FROM articles a WHERE a.archived=0 AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.article_id=a.id AND c.version=a.version AND c.config_fingerprint=?)"
            params: list[Any] = [fingerprint]
            if article_id is not None:
                skipped_sql += " AND a.id=?"
                skipped_params.append(article_id)
                sql += " AND a.id=?"
                params.append(article_id)
            skipped = int(db.execute(skipped_sql, skipped_params).fetchone()[0])
            rows = db.execute(sql, params).fetchall()
            for article in rows:
                existing = db.execute(
                    "SELECT id,config_fingerprint FROM chunks WHERE article_id=? AND version=?",
                    (article["id"], article["version"]),
                ).fetchall()
                if existing and all(row["config_fingerprint"] == fingerprint for row in existing):
                    skipped += len(existing)
                    continue
                if existing:
                    for row in existing:
                        db.execute("DELETE FROM chunk_fts WHERE chunk_id=?", (row["id"],))
                    db.execute("DELETE FROM chunks WHERE article_id=? AND version=?", (article["id"], article["version"]))
                    updated += 1
                chunks = chunk_document(article["content"], article_id=article["id"], version=article["version"], max_chars=max_chars)
                for chunk in chunks:
                    db.execute(
                        "INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (chunk["id"], chunk["article_id"], chunk["version"], chunk["text"], chunk["text_hash"], _json(chunk["heading_path"]), chunk["start"], chunk["end"], chunk["ordinal"], chunk["previous_id"], chunk["next_id"], fingerprint, time.time()),
                    )
                    db.execute("INSERT INTO chunk_fts(chunk_id,text,symbols) VALUES(?,?,?)", (chunk["id"], chunk["text"], " ".join(_terms(chunk["text"]))))
                created += len(chunks)
            db.commit()
        return {"created": created, "updated": updated, "skipped": skipped}

    def list_chunks(self, article_id: str | None = None, *, current_only: bool = True, limit: int | None = None) -> list[dict]:
        sql = "SELECT c.* FROM chunks c JOIN articles a ON a.id=c.article_id WHERE 1=1"
        params: list[Any] = []
        if current_only:
            sql += " AND c.version=a.version AND a.archived=0"
        if article_id is not None:
            sql += " AND c.article_id=?"
            params.append(article_id)
        sql += " ORDER BY c.article_id,c.version,c.ordinal"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._connection() as db:
            rows = db.execute(sql, params).fetchall()
            return [self._chunk_enriched(db, row) for row in rows]

    def get_chunk(self, chunk_id: str) -> dict | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM chunks WHERE id=?", (chunk_id,)).fetchone()
            return self._chunk_enriched(db, row) if row else None

    def search_chunks(self, query: str, limit: int = 30, source: str | None = None) -> list[dict]:
        if limit <= 0 or not query.strip():
            return []
        terms = _terms(query)
        matched: dict[str, tuple[float, str]] = {}
        with self._connection() as db:
            if terms:
                match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
                sql = "SELECT f.chunk_id,bm25(chunk_fts) AS rank,snippet(chunk_fts,1,'<b>','</b>','…',16) AS snippet FROM chunk_fts f JOIN chunks c ON c.id=f.chunk_id JOIN articles a ON a.id=c.article_id WHERE chunk_fts MATCH ? AND a.archived=0 AND c.version=a.version"
                params: list[Any] = [match]
                if source:
                    sql += " AND a.source=?"
                    params.append(source)
                sql += " ORDER BY rank LIMIT ?"
                params.append(limit)
                rows = db.execute(sql, params).fetchall()
                for row in rows:
                    matched[row["chunk_id"]] = (-float(row["rank"]), row["snippet"])
            # Literal matching recovers exact symbols and Chinese phrases that tokenization misses.
            sql = "SELECT c.* FROM chunks c JOIN articles a ON a.id=c.article_id WHERE a.archived=0 AND a.version=c.version AND instr(lower(c.text),lower(?))>0"
            params = [query]
            if source:
                sql += " AND a.source=?"
                params.append(source)
            sql += " LIMIT ?"
            params.append(limit)
            for row in db.execute(sql, params):
                matched[row["id"]] = (1000.0 + len(query), query)
            if not matched:
                return []
            ids = sorted(matched, key=lambda chunk_id: matched[chunk_id][0], reverse=True)[:limit]
            items = []
            for start in range(0, len(ids), 800):
                batch = ids[start : start + 800]
                placeholders = ",".join("?" for _ in batch)
                sql = f"SELECT c.* FROM chunks c JOIN articles a ON a.id=c.article_id WHERE c.id IN ({placeholders}) AND a.archived=0 AND a.version=c.version"
                params = list(batch)
                if source:
                    sql += " AND a.source=?"
                    params.append(source)
                rows = db.execute(sql, params).fetchall()
                for row in rows:
                    item = self._chunk_enriched(db, row)
                    item["score"], item["snippet"] = matched[row["id"]]
                    items.append(item)
            items.sort(key=lambda item: item["score"], reverse=True)
            return items

    def get_embeddings(self, model_key: str, text_hashes: list[str]) -> dict[str, list[float]]:
        if not text_hashes:
            return {}
        output: dict[str, list[float]] = {}
        with self._connection() as db:
            for start in range(0, len(text_hashes), 800):
                batch = text_hashes[start : start + 800]
                placeholders = ",".join("?" for _ in batch)
                rows = db.execute(f"SELECT text_hash,vector_json FROM embeddings WHERE model_key=? AND text_hash IN ({placeholders})", [model_key, *batch])
                output.update({row["text_hash"]: json.loads(row["vector_json"]) for row in rows})
        return output

    def get_embedding_manifest(self, model_key: str) -> dict | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM embedding_manifests WHERE model_key=?", (model_key,)).fetchone()
            return dict(row) if row else None

    def has_embedding_manifest(self, model_key: str) -> bool:
        return self.get_embedding_manifest(model_key) is not None

    def save_embeddings(self, model_key: str, model: str, digest: str, dimension: int, items: list[dict]) -> None:
        if not model_key or not model or not digest or dimension <= 0:
            raise ValueError("model identity and positive dimension are required")
        normalized = []
        for item in items:
            vector = item["vector"]
            if len(vector) != dimension or any(not math.isfinite(float(x)) for x in vector) or not any(float(x) != 0 for x in vector):
                raise ValueError("embedding vectors must be finite, nonzero and match the manifest dimension")
            normalized.append((item["text_hash"], _json([float(x) for x in vector])))
        with self._write_lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            manifest = db.execute("SELECT * FROM embedding_manifests WHERE model_key=?", (model_key,)).fetchone()
            if manifest and (manifest["model"], manifest["digest"], manifest["dimension"]) != (model, digest, dimension):
                db.rollback()
                raise ValueError("model_key already identifies a different embedding manifest")
            db.execute("INSERT OR IGNORE INTO embedding_manifests VALUES(?,?,?,?,?)", (model_key, model, digest, dimension, time.time()))
            db.executemany("INSERT INTO embeddings VALUES(?,?,?,?) ON CONFLICT(model_key,text_hash) DO UPDATE SET vector_json=excluded.vector_json", [(model_key, h, vector, time.time()) for h, vector in normalized])
            db.commit()

    def vector_search(self, vector: list[float], model_key: str, limit: int = 30, source: str | None = None) -> list[dict]:
        if not vector or limit <= 0 or any(not math.isfinite(float(x)) for x in vector):
            return []
        norm = math.sqrt(sum(float(x) ** 2 for x in vector))
        if not norm:
            raise ValueError("query vector must be nonzero")
        with self._connection() as db:
            manifest = db.execute("SELECT dimension FROM embedding_manifests WHERE model_key=?", (model_key,)).fetchone()
            if manifest is None:
                return []
            if manifest["dimension"] != len(vector):
                raise ValueError("query vector dimension does not match model manifest")
            sql = "SELECT c.id,e.vector_json FROM chunks c JOIN articles a ON a.id=c.article_id JOIN embeddings e ON e.model_key=? AND e.text_hash=c.text_hash WHERE a.archived=0 AND c.version=a.version"
            params: list[Any] = [model_key]
            if source:
                sql += " AND a.source=?"
                params.append(source)
            top: list[tuple[float, str]] = []
            for row in db.execute(sql, params):
                candidate = json.loads(row["vector_json"])
                if len(candidate) != len(vector):
                    continue
                candidate_norm = math.sqrt(sum(x * x for x in candidate))
                if not candidate_norm:
                    continue
                score = sum(float(a) * float(b) for a, b in zip(vector, candidate)) / (norm * candidate_norm)
                entry = (score, row["id"])
                if len(top) < limit:
                    heapq.heappush(top, entry)
                elif entry > top[0]:
                    heapq.heapreplace(top, entry)
            top.sort(reverse=True)
            if not top:
                return []
            identifiers = [chunk_id for _score, chunk_id in top]
            placeholders = ",".join("?" for _ in identifiers)
            rows = db.execute(
                f"SELECT c.*,a.title AS article_title,a.url,a.source,a.archived,a.version AS current_version,v.raw_path FROM chunks c JOIN articles a ON a.id=c.article_id JOIN article_versions v ON v.article_id=c.article_id AND v.version=c.version WHERE c.id IN ({placeholders})",
                identifiers,
            ).fetchall()
            by_id = {}
            for row in rows:
                item = dict(row)
                item["heading_path"] = json.loads(item.pop("heading_path_json"))
                item["start"] = item.pop("start_offset")
                item["end"] = item.pop("end_offset")
                item["title"] = item.pop("article_title")
                item["current"] = item["version"] == item.pop("current_version")
                item["archived"] = bool(item["archived"])
                by_id[item["id"]] = item
            items = []
            for score, chunk_id in top:
                item = by_id[chunk_id]
                item["score"] = score
                items.append(item)
            return items

    def save_extraction(self, article_id: str, version: int, model: str, model_digest: str, payload: dict, status: str = "ai_extracted", error: str | None = None) -> dict:
        extraction_id = uuid.uuid4().hex
        now = time.time()
        cards = payload.get("cards", []) if isinstance(payload, dict) else []
        with self._write_lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM article_versions WHERE article_id=? AND version=?", (article_id, version)).fetchone():
                db.rollback()
                raise ValueError("article version does not exist")
            db.execute("INSERT INTO extractions VALUES(?,?,?,?,?,?,?,?,?)", (extraction_id, article_id, version, model, model_digest, status, _json(payload), error, now))
            if status != "failed":
                for card in cards:
                    if not isinstance(card, dict):
                        continue
                    card_id = str(card.get("id") or uuid.uuid4().hex)
                    all_evidence = payload.get("evidence", [])
                    indices = card.get("evidence_indices", [])
                    if any(not isinstance(index, int) or index < 0 or index >= len(all_evidence) for index in indices):
                        db.rollback()
                        raise ValueError("knowledge card references an unknown evidence index")
                    evidence = [all_evidence[index] for index in indices]
                    db.execute("INSERT INTO knowledge_cards(id,extraction_id,article_id,version,title,content,evidence_json,confidence,verification_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (card_id, extraction_id, article_id, version, str(card.get("title") or card.get("direction") or "Knowledge"), str(card.get("content") or card.get("summary") or ""), _json(evidence), card.get("confidence", payload.get("confidence")), "unverified", now))
            db.commit()
        return {"id": extraction_id, "article_id": article_id, "version": version, "model": model, "model_digest": model_digest, "status": status, "payload": payload, "error": error, "created_at": now}

    def list_extractions(self, article_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM extractions"
        params: tuple = ()
        if article_id is not None:
            sql += " WHERE article_id=?"
            params = (article_id,)
        sql += " ORDER BY created_at DESC"
        with self._connection() as db:
            return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in db.execute(sql, params)]

    def list_knowledge(self, limit: int = 50, offset: int = 0) -> list[dict]:
        with self._connection() as db:
            rows = db.execute("SELECT k.*,e.model,e.model_digest,a.url,a.url AS source_url,a.title AS article_title,v.raw_path FROM knowledge_cards k JOIN extractions e ON e.id=k.extraction_id JOIN articles a ON a.id=k.article_id JOIN article_versions v ON v.article_id=k.article_id AND v.version=k.version ORDER BY k.created_at DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            return [{**dict(row), "evidence": json.loads(row["evidence_json"])} for row in rows]

    def get_knowledge(self, card_id: str) -> dict | None:
        with self._connection() as db:
            row = db.execute("SELECT k.*,e.model,e.model_digest,a.url,a.url AS source_url,a.title AS article_title,v.raw_path FROM knowledge_cards k JOIN extractions e ON e.id=k.extraction_id JOIN articles a ON a.id=k.article_id JOIN article_versions v ON v.article_id=k.article_id AND v.version=k.version WHERE k.id=?", (card_id,)).fetchone()
            return {**dict(row), "evidence": json.loads(row["evidence_json"])} if row else None
