from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

ENTITY_KINDS = {"challenge", "writeup", "vulnerability", "root_cause", "attack", "fix", "exploit", "patch", "knowledge", "tool", "precondition", "verification"}
PROVENANCE = {"source_claimed", "ai_extracted", "human_authored"}
RELATION_ENDPOINTS = {
    "challenge_writeup": ("challenge", "writeup"), "challenge_vulnerability": ("challenge", "vulnerability"),
    "writeup_knowledge": ("writeup", "knowledge"), "writeup_attack": ("writeup", "attack"), "writeup_fix": ("writeup", "fix"),
    "vulnerability_root_cause": ("vulnerability", "root_cause"), "vulnerability_attack": ("vulnerability", "attack"),
    "vulnerability_fix": ("vulnerability", "fix"), "vulnerability_knowledge": ("vulnerability", "knowledge"),
    "attack_precondition": ("attack", "precondition"), "attack_exploit": ("attack", "exploit"), "attack_fix": ("attack", "fix"),
    "fix_patch": ("fix", "patch"), "fix_verification": ("fix", "verification"), "knowledge_tool": ("knowledge", "tool"),
}


class RevisionConflict(ValueError):
    """Raised when a conditional update targets a stale revision."""


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _obj(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("attributes_json", "evidence_json", "before_json", "after_json"):
        if key in d:
            d[key.removesuffix("_json")] = json.loads(d.pop(key))
    return d


def _base_evidence(items: list[dict]) -> list[dict]:
    keys=("article_id","version","start","end","quote")
    return [{key:item[key] for key in keys} for item in items]


class KnowledgeStorageMixin:
    def _initialize_stage3(self) -> None:
        with self._connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS stage3_schema_migrations(version INTEGER PRIMARY KEY,applied_at REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS knowledge_card_revisions(card_id TEXT PRIMARY KEY REFERENCES knowledge_cards(id),revision INTEGER NOT NULL DEFAULT 1,updated_at REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS knowledge_card_edits(id TEXT PRIMARY KEY,card_id TEXT NOT NULL REFERENCES knowledge_cards(id),revision INTEGER NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL,created_at REAL NOT NULL)")
            if db.execute("SELECT 1 FROM stage3_schema_migrations WHERE version=1").fetchone():
                return
            db.executescript("""
            CREATE TABLE IF NOT EXISTS entities(id TEXT PRIMARY KEY,kind TEXT NOT NULL,title TEXT NOT NULL,description TEXT NOT NULL,attributes_json TEXT NOT NULL,evidence_json TEXT NOT NULL,provenance TEXT NOT NULL,verification_status TEXT NOT NULL,revision INTEGER NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS entities_kind_idx ON entities(kind,title); CREATE INDEX IF NOT EXISTS entities_status_idx ON entities(verification_status);
            CREATE TABLE IF NOT EXISTS relations(id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES entities(id),target_id TEXT NOT NULL REFERENCES entities(id),kind TEXT NOT NULL,rationale TEXT NOT NULL,evidence_json TEXT NOT NULL,provenance TEXT NOT NULL,status TEXT NOT NULL,revision INTEGER NOT NULL,source_revision INTEGER NOT NULL,target_revision INTEGER NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL,UNIQUE(source_id,target_id,kind));
            CREATE INDEX IF NOT EXISTS relations_source_idx ON relations(source_id,kind); CREATE INDEX IF NOT EXISTS relations_target_idx ON relations(target_id,kind);
            CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY,target_type TEXT NOT NULL,target_id TEXT NOT NULL,decision TEXT NOT NULL,note TEXT NOT NULL,reviewer TEXT NOT NULL,target_revision INTEGER NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL,created_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS reviews_target_idx ON reviews(target_type,target_id,created_at);
            """)
            db.execute("INSERT INTO stage3_schema_migrations VALUES(1,?)", (time.time(),))

    def _evidence(self, db: sqlite3.Connection, evidence: Any, *, required: bool) -> list[dict]:
        if not isinstance(evidence, list) or (required and not evidence):
            raise ValueError("evidence must be a non-empty list" if required else "evidence must be a list")
        result=[]
        for item in evidence:
            if not isinstance(item, dict): raise ValueError("evidence entries must be objects")  # noqa: TRY004 - public validation contract uses ValueError
            try: aid, version, start, end, quote = item["article_id"], item["version"], item["start"], item["end"], item["quote"]
            except KeyError as exc: raise ValueError(f"missing evidence field: {exc.args[0]}") from exc
            if type(version) is not int or type(start) is not int or type(end) is not int or start < 0 or end <= start or not isinstance(quote,str) or not quote:
                raise ValueError("invalid evidence range or quote")
            row=db.execute("SELECT v.content,v.raw_path,a.url,v.title FROM article_versions v JOIN articles a ON a.id=v.article_id WHERE v.article_id=? AND v.version=?",(aid,version)).fetchone()
            if not row or end > len(row["content"]) or row["content"][start:end] != quote: raise ValueError("evidence must exactly match a stored article version range")
            result.append({"article_id":aid,"version":version,"start":start,"end":end,"quote":quote,"source_url":row["url"],"raw_path":row["raw_path"],"title":row["title"],"source_status":"source_claimed"})
        return result

    def _entity(self, db, ident):
        row=db.execute("SELECT * FROM entities WHERE id=?",(ident,)).fetchone()
        if not row:return None
        d=_obj(row)
        d["evidence"]=self._evidence(db,d["evidence"],required=False)
        return d

    def create_entity(self,payload:dict)->dict:
        kind=payload.get("kind"); title=payload.get("title")
        provenance=payload.get("provenance","human_authored")
        if kind not in ENTITY_KINDS or not isinstance(title,str) or not title.strip() or provenance not in PROVENANCE:raise ValueError("valid kind, provenance and non-empty title are required")
        if not isinstance(payload.get("attributes",{}),dict):raise ValueError("attributes must be an object")  # noqa: TRY004
        now=time.time(); ident=str(payload.get("id") or uuid.uuid4().hex)
        with self._write_lock,self._connection() as db:
            db.execute("BEGIN IMMEDIATE"); ev=self._evidence(db,payload.get("evidence",[]),required=False)
            db.execute("INSERT INTO entities VALUES(?,?,?,?,?,?,?,?,?,?,?)",(ident,kind,title.strip(),str(payload.get("description", "")),_j(payload.get("attributes",{})),_j(_base_evidence(ev)),provenance,"unverified",1,now,now)); db.commit()
            return self._entity(db,ident)

    def update_entity(self,entity_id:str,payload:dict,*,expected_revision:int)->dict:
        now=time.time()
        with self._write_lock,self._connection() as db:
            db.execute("BEGIN IMMEDIATE"); old=self._entity(db,entity_id)
            if old is None:db.rollback();return None
            if old["revision"]!=expected_revision:db.rollback();raise RevisionConflict(f"stale entity revision: expected {expected_revision}, current {old['revision']}")
            kind=payload.get("kind",old["kind"]); title=payload.get("title",old["title"])
            provenance=payload.get("provenance",old["provenance"])
            if kind not in ENTITY_KINDS or not isinstance(title,str) or not title.strip() or provenance not in PROVENANCE:db.rollback();raise ValueError("invalid entity kind/title/provenance")
            if not isinstance(payload.get("attributes",old["attributes"]),dict):db.rollback();raise ValueError("attributes must be an object")  # noqa: TRY004
            if kind!=old["kind"] and db.execute("SELECT 1 FROM relations WHERE source_id=? OR target_id=? LIMIT 1",(entity_id,entity_id)).fetchone():
                db.rollback();raise ValueError("cannot change entity kind while relations reference it")
            ev=self._evidence(db,payload.get("evidence",old["evidence"]),required=False)
            db.execute("UPDATE entities SET kind=?,title=?,description=?,attributes_json=?,evidence_json=?,provenance=?,verification_status='unverified',revision=revision+1,updated_at=? WHERE id=?",(kind,title,str(payload.get("description",old["description"])),_j(payload.get("attributes",old["attributes"])),_j(_base_evidence(ev)),provenance,now,entity_id))
            db.commit();return self._entity(db,entity_id)

    def get_entity(self,entity_id:str)->dict|None:
        with self._connection() as db:return self._entity(db,entity_id)

    def list_entities(self,*,kind=None,q=None,limit=50,offset=0,filters=None)->list[dict]:
        if limit<0 or offset<0:raise ValueError("limit and offset must be non-negative")
        filters=filters or {}; where=[]; args=[]
        if kind:where.append("kind=?");args.append(kind)
        if q:where.append("(title LIKE ? OR description LIKE ? OR attributes_json LIKE ?)");args.extend([f"%{q}%"]*3)
        allowed={"competition","year","category","vulnerability_type","verification_status","source"}
        unknown=set(filters)-allowed
        if unknown:raise ValueError(f"unsupported entity filter: {min(unknown)}")
        for key in ("competition","year","category","vulnerability_type"):
            if key in filters:
                if key=="year":where.append("CAST(json_extract(attributes_json,'$.year') AS TEXT)=CAST(? AS TEXT)");args.append(filters[key])
                else:where.append("json_extract(attributes_json,?)=?");args.extend([f'$.{key}',filters[key]])
        if "verification_status" in filters:where.append("verification_status=?");args.append(filters["verification_status"])
        if "source" in filters:
            where.append("EXISTS (SELECT 1 FROM json_each(entities.evidence_json) ev JOIN articles a ON a.id=json_extract(ev.value,'$.article_id') WHERE a.source=?)");args.append(filters["source"])
        sql="SELECT id FROM entities"+(" WHERE "+" AND ".join(where) if where else "")+" ORDER BY updated_at DESC,id LIMIT ? OFFSET ?";args.extend([limit,offset])
        with self._connection() as db:return [self._entity(db,r[0]) for r in db.execute(sql,args)]

    def create_relation(self,payload:dict)->dict:
        kind=payload.get("kind"); endpoints=RELATION_ENDPOINTS.get(kind)
        if not endpoints:raise ValueError("unsupported relation kind")
        if payload.get("provenance","ai_extracted") not in PROVENANCE:raise ValueError("invalid relation provenance")
        now=time.time(); ident=str(payload.get("id") or uuid.uuid4().hex)
        with self._write_lock,self._connection() as db:
            db.execute("BEGIN IMMEDIATE"); source=self._entity(db,payload.get("source_id"));target=self._entity(db,payload.get("target_id"))
            if not source or not target or source["kind"]!=endpoints[0] or target["kind"]!=endpoints[1] or source["id"]==target["id"]:db.rollback();raise ValueError("relation endpoints do not match declared kinds")
            ev=self._evidence(db,payload.get("evidence"),required=True)
            existing=db.execute("SELECT id,evidence_json,rationale FROM relations WHERE source_id=? AND target_id=? AND kind=?",(source["id"],target["id"],kind)).fetchone()
            if existing:
                if existing["evidence_json"]==_j(_base_evidence(ev)) and existing["rationale"]==str(payload.get("rationale","")):db.commit();return self.get_relation(existing["id"])
                db.rollback();raise ValueError("semantic relation exists; update it explicitly")
            db.execute("INSERT INTO relations VALUES(?,?,?,?,?,?,?,'candidate',1,?,?,?,?)",(ident,source["id"],target["id"],kind,str(payload.get("rationale","")),_j(_base_evidence(ev)),payload.get("provenance","ai_extracted"),source["revision"],target["revision"],now,now));db.commit();return self.get_relation(ident)

    def _relation(self,db,ident):
        row=db.execute("SELECT * FROM relations WHERE id=?",(ident,)).fetchone()
        if not row:return None
        d=_obj(row); d["evidence"]=self._evidence(db,d["evidence"],required=True);d["source"]=self._entity(db,d["source_id"]);d["target"]=self._entity(db,d["target_id"])
        d["stale_endpoints"]=d["source_revision"]!=d["source"]["revision"] or d["target_revision"]!=d["target"]["revision"]
        if d["stale_endpoints"] and d["status"]=="human_verified":d["status"]="candidate"
        return d

    def get_relation(self,relation_id:str)->dict|None:
        with self._connection() as db:return self._relation(db,relation_id)

    def list_relations(self,*,entity_id=None,kind=None,status=None,limit:int|None=50,offset=0)->list[dict]:
        if (limit is not None and limit<0) or offset<0:raise ValueError("limit and offset must be non-negative")
        w=[];a=[]
        if entity_id:w.append("(source_id=? OR target_id=?)");a.extend([entity_id,entity_id])
        if kind:w.append("kind=?");a.append(kind)
        if status=="human_verified":w.append("status='human_verified' AND source_revision=(SELECT revision FROM entities WHERE id=source_id) AND target_revision=(SELECT revision FROM entities WHERE id=target_id)")
        elif status=="candidate":w.append("(status='candidate' OR (status='human_verified' AND (source_revision!=(SELECT revision FROM entities WHERE id=source_id) OR target_revision!=(SELECT revision FROM entities WHERE id=target_id))))")
        elif status:w.append("status=?");a.append(status)
        sql="SELECT id FROM relations"+(" WHERE "+" AND ".join(w) if w else "")+" ORDER BY updated_at DESC,id"
        if limit is not None:sql+=" LIMIT ? OFFSET ?";a.extend([limit,offset])
        elif offset:sql+=" LIMIT -1 OFFSET ?";a.append(offset)
        with self._connection() as db:return [self._relation(db,r[0]) for r in db.execute(sql,a)]

    def update_relation(self,relation_id,payload,*,expected_revision:int)->dict|None:
        with self._write_lock,self._connection() as db:
            db.execute("BEGIN IMMEDIATE");old=db.execute("SELECT * FROM relations WHERE id=?",(relation_id,)).fetchone()
            if not old:db.rollback();return None
            if old["revision"]!=expected_revision:db.rollback();raise RevisionConflict("stale relation revision")
            row=self._relation(db,relation_id); kind=payload.get("kind",row["kind"]); endpoints=RELATION_ENDPOINTS.get(kind)
            provenance=payload.get("provenance",row["provenance"])
            source=self._entity(db,payload.get("source_id",row["source_id"]));target=self._entity(db,payload.get("target_id",row["target_id"]))
            if not endpoints or not source or not target or (source["kind"],target["kind"])!=endpoints or provenance not in PROVENANCE:db.rollback();raise ValueError("relation endpoints/provenance do not match declared contract")
            ev=self._evidence(db,payload.get("evidence",row["evidence"]),required=True)
            db.execute("UPDATE relations SET source_id=?,target_id=?,kind=?,rationale=?,evidence_json=?,provenance=?,status='candidate',source_revision=?,target_revision=?,revision=revision+1,updated_at=? WHERE id=?",(source["id"],target["id"],kind,str(payload.get("rationale",row["rationale"])),_j(_base_evidence(ev)),provenance,source["revision"],target["revision"],time.time(),relation_id));db.commit();return self.get_relation(relation_id)

    def related(self,entity_id,*,depth:int|None=1,limit:int|None=50)->dict:
        if depth is not None and depth<0:raise ValueError("depth must be non-negative")
        if limit is not None and limit<0:raise ValueError("limit must be non-negative")
        if not self.get_entity(entity_id):return {"entities":[],"relations":[],"warnings":["entity_not_found"]}
        seen={entity_id};edges_by_id={};front={entity_id};warn=[];level=0
        while front and (depth is None or level<depth):
            nxt=set()
            for ident in front:
                relation_limit=None if limit is None else limit+1
                for rel in self.list_relations(entity_id=ident,limit=relation_limit):
                    if rel["status"]=="rejected":continue
                    if rel["id"] in edges_by_id:continue
                    if limit is not None and len(edges_by_id)>=limit:warn.append("relation_limit_reached");break
                    edges_by_id[rel["id"]]=rel; other=rel["target_id"] if rel["source_id"]==ident else rel["source_id"]
                    if other not in seen:
                        if limit is not None and len(seen)>=limit:warn.append("entity_limit_reached");continue
                        seen.add(other);nxt.add(other)
            if not nxt:break
            front=nxt
            level+=1
        if front and depth is not None and level>=depth:warn.append("depth_limit_reached")
        with self._connection() as db: ents=[self._entity(db,i) for i in seen]
        return {"entities":ents,"relations":list(edges_by_id.values()),"warnings":warn}

    def find_attack_fix(self,query:str="",*,entity_id:str|None=None,limit:int=20)->dict:
        seeds=[self.get_entity(entity_id)] if entity_id else self.list_entities(q=query,limit=limit)
        seeds=[x for x in seeds if x]
        matches=[]; allrels={}; seen={}; queue=[seed["id"] for seed in seeds]
        while queue:
            ident=queue.pop()
            if ident in seen:continue
            seen[ident]=True
            for rel in self.list_relations(entity_id=ident,limit=2147483647):
                if rel["status"]=="rejected":continue
                allrels[rel["id"]]=rel; nxt=rel["target"] if rel["source_id"]==ident else rel["source"]
                if nxt and nxt["id"] not in seen:queue.append(nxt["id"])
        ents={e["id"]:e for e in (self.get_entity(i) for i in seen)}
        for rel in allrels.values():
            if rel["kind"]!="attack_fix":continue
            attack=ents.get(rel["source_id"]);fix=ents.get(rel["target_id"])
            if not attack or not fix:continue
            patches=[r["target"] for r in allrels.values() if r["kind"]=="fix_patch" and r["source_id"]==fix["id"]]
            matches.append({"attack_id":attack["id"],"fix_id":fix["id"],"patch_ids":[p["id"] for p in patches],"rationale":rel["rationale"],"evidence":rel["evidence"],"status":rel["status"],"patches":patches})
        warnings=[]
        if len(ents)>limit:warnings.append("entity_results_truncated")
        if len(allrels)>limit:warnings.append("relation_results_truncated")
        if len(matches)>limit:warnings.append("comparison_results_truncated")
        return {"entities":list(ents.values())[:limit],"relations":list(allrels.values())[:limit],"comparisons":matches[:limit],"warnings":warnings}

    def review_record(self,target_type,target_id,decision,*,note,reviewer,expected_revision)->dict:
        if target_type not in {"entity","relation","knowledge"} or decision not in {"human_verified","rejected"} or not note or not reviewer:raise ValueError("valid target, decision, note and reviewer required")
        with self._write_lock,self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            table="relations" if target_type=="relation" else ("knowledge_cards" if target_type=="knowledge" else "entities")
            row=db.execute(f"SELECT * FROM {table} WHERE id=?",(target_id,)).fetchone()
            if not row:db.rollback();return None
            before=dict(row)
            if target_type=="knowledge":
                revision=db.execute("SELECT revision FROM knowledge_card_revisions WHERE card_id=?",(target_id,)).fetchone()
                rev=revision[0] if revision else 1
            else:rev=row["revision"]
            if rev!=expected_revision:db.rollback();raise RevisionConflict("stale review revision")
            if target_type!="relation" and decision=="human_verified":
                if target_type=="entity":
                    if not self._evidence(db,json.loads(row["evidence_json"]),required=True):db.rollback();raise ValueError("human verification requires evidence")
                else:
                    original=db.execute("SELECT content FROM article_versions WHERE article_id=? AND version=?",(row["article_id"],row["version"])).fetchone()
                    evidence=json.loads(row["evidence_json"])
                    if not original or not evidence:db.rollback();raise ValueError("human verification requires evidence")
                    for item in evidence:
                        if not isinstance(item,dict) or type(item.get("start")) is not int or type(item.get("end")) is not int or item["start"]<0 or item["end"]<=item["start"] or original["content"][item["start"]:item["end"]]!=item.get("quote"):
                            db.rollback();raise ValueError("knowledge evidence must exactly match its stored article version")
            col="status" if target_type=="relation" else "verification_status"
            if target_type=="relation" and decision=="human_verified":
                db.execute("UPDATE relations SET status=?,source_revision=(SELECT revision FROM entities WHERE id=source_id),target_revision=(SELECT revision FROM entities WHERE id=target_id) WHERE id=?",(decision,target_id))
            else:db.execute(f"UPDATE {table} SET {col}=? WHERE id=?",(decision,target_id))
            after=dict(db.execute(f"SELECT * FROM {table} WHERE id=?",(target_id,)).fetchone())
            if target_type=="knowledge":after["revision"]=rev
            db.execute("INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?,?,?)",(uuid.uuid4().hex,target_type,target_id,decision,note,reviewer,rev,_j(before),_j(after),time.time()));db.commit()
            return {"target_type":target_type,"target_id":target_id,"decision":decision,"revision":rev,"before":before,"after":after}

    def list_reviews(self,*,target_id=None,limit=100,offset=0)->list[dict]:
        if limit<0 or offset<0:raise ValueError("limit and offset must be non-negative")
        target_filter="WHERE target_id=?" if target_id else ""
        edit_filter="WHERE card_id=?" if target_id else ""
        args=([target_id] if target_id else [])+([target_id] if target_id else [])+[limit,offset]
        sql=f"""
            SELECT * FROM (
                SELECT 'review' AS record_type,id,target_type,target_id,decision,note,reviewer,target_revision,before_json,after_json,created_at
                FROM reviews {target_filter}
                UNION ALL
                SELECT 'knowledge_edit',id,'knowledge_edit',card_id,'edited','Knowledge card edited; extracted source retained',NULL,revision,before_json,after_json,created_at
                FROM knowledge_card_edits {edit_filter}
            ) ORDER BY created_at DESC,record_type,id LIMIT ? OFFSET ?
        """
        with self._connection() as db:
            return [{"id":r["id"],"target_type":r["target_type"],"target_id":r["target_id"],"decision":r["decision"],"note":r["note"],"reviewer":r["reviewer"],"target_revision":r["target_revision"],"before":json.loads(r["before_json"]),"after":json.loads(r["after_json"]),"created_at":r["created_at"]} for r in db.execute(sql,args)]

    def pending_reviews(self,limit=50,offset=0)->list[dict]:
        if limit<0 or offset<0:raise ValueError("limit and offset must be non-negative")
        with self._connection() as db:
            rows=db.execute("""
                SELECT target_type,target_id FROM (
                    SELECT 'entity' AS target_type,id AS target_id,created_at FROM entities WHERE verification_status='unverified'
                    UNION ALL
                    SELECT 'relation',id,created_at FROM relations WHERE status='candidate' OR
                      (status='human_verified' AND (source_revision!=(SELECT revision FROM entities WHERE id=source_id)
                       OR target_revision!=(SELECT revision FROM entities WHERE id=target_id)))
                    UNION ALL
                    SELECT 'knowledge',id,created_at FROM knowledge_cards WHERE verification_status='unverified'
                ) ORDER BY created_at,target_type,target_id LIMIT ? OFFSET ?
            """,(limit,offset)).fetchall()
            results=[]
            for row in rows:
                if row["target_type"]=="entity":record=self._entity(db,row["target_id"])
                elif row["target_type"]=="relation":record=self._relation(db,row["target_id"])
                else:
                    record=dict(db.execute("SELECT * FROM knowledge_cards WHERE id=?",(row["target_id"],)).fetchone())
                    revision=db.execute("SELECT revision FROM knowledge_card_revisions WHERE card_id=?",(row["target_id"],)).fetchone();record["revision"]=revision[0] if revision else 1
                results.append({"target_type":row["target_type"],"record":record})
            return results

    def update_knowledge(self,knowledge_id,payload,*,expected_revision:int)->dict|None:
        with self._write_lock,self._connection() as db:
            db.execute("BEGIN IMMEDIATE");row=db.execute("SELECT * FROM knowledge_cards WHERE id=?",(knowledge_id,)).fetchone()
            if not row:db.rollback();return None
            revision=db.execute("SELECT revision FROM knowledge_card_revisions WHERE card_id=?",(knowledge_id,)).fetchone();current=revision[0] if revision else 1
            if current!=expected_revision:db.rollback();raise RevisionConflict("stale knowledge revision")
            title=payload.get("title",row["title"]);content=payload.get("content",row["content"])
            if not isinstance(title,str) or not title.strip() or not isinstance(content,str):db.rollback();raise ValueError("title/content invalid")
            before=dict(row);now=time.time()
            db.execute("UPDATE knowledge_cards SET title=?,content=?,verification_status='unverified' WHERE id=?",(title,content,knowledge_id));db.execute("INSERT INTO knowledge_card_revisions(card_id,revision,updated_at) VALUES(?,?,?) ON CONFLICT(card_id) DO UPDATE SET revision=excluded.revision,updated_at=excluded.updated_at",(knowledge_id,current+1,now));after=dict(db.execute("SELECT * FROM knowledge_cards WHERE id=?",(knowledge_id,)).fetchone());after["revision"]=current+1
            db.execute("INSERT INTO knowledge_card_edits VALUES(?,?,?,?,?,?)",(uuid.uuid4().hex,knowledge_id,current+1,_j(before),_j(after),now));db.commit()
            result=dict(db.execute("SELECT * FROM knowledge_cards WHERE id=?",(knowledge_id,)).fetchone());result["revision"]=current+1;return result

    def knowledge_stats(self)->dict:
        with self._connection() as db:
            return {"entities_by_kind":{r[0]:r[1] for r in db.execute("SELECT kind,count(*) FROM entities GROUP BY kind")},"relations_by_kind":{r[0]:r[1] for r in db.execute("SELECT kind,count(*) FROM relations GROUP BY kind")},"relation_status":{r[0]:r[1] for r in db.execute("SELECT CASE WHEN status='human_verified' AND source_revision=(SELECT revision FROM entities WHERE id=source_id) AND target_revision=(SELECT revision FROM entities WHERE id=target_id) THEN 'human_verified' WHEN status='rejected' THEN 'rejected' ELSE 'candidate' END AS effective_status,count(*) FROM relations GROUP BY effective_status")},"entity_status":{r[0]:r[1] for r in db.execute("SELECT verification_status,count(*) FROM entities GROUP BY verification_status")},"reviews":db.execute("SELECT count(*) FROM reviews").fetchone()[0],"knowledge_edits":db.execute("SELECT count(*) FROM knowledge_card_edits").fetchone()[0],"knowledge_cards":db.execute("SELECT count(*) FROM knowledge_cards").fetchone()[0],"chunks":db.execute("SELECT count(*) FROM chunks").fetchone()[0],"vectors":db.execute("SELECT count(*) FROM embeddings").fetchone()[0],"embedding_models":db.execute("SELECT count(*) FROM embedding_manifests").fetchone()[0],"extractions":db.execute("SELECT count(*) FROM extractions").fetchone()[0]}
