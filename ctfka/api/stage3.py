from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field


class EntityBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    kind: str
    title: str
    description: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    provenance: str = "human_authored"


class RelationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    source_id: str
    target_id: str
    kind: str
    rationale: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    provenance: str = "human_authored"


class ChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1)
    entity_id: str | None = None
    model: str | None = None


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_type: str
    target_id: str
    decision: str
    note: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    expected_revision: int = Field(ge=1, strict=True)


class EntityPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(gt=0, strict=True)
    kind: str | None = None
    title: str | None = None
    description: str | None = None
    attributes: dict[str, Any] | None = None
    evidence: list[dict[str, Any]] | None = None
    provenance: str | None = None


class RelationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(gt=0, strict=True)
    source_id: str | None = None
    target_id: str | None = None
    kind: str | None = None
    rationale: str | None = None
    evidence: list[dict[str, Any]] | None = None
    provenance: str | None = None


class KnowledgePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(gt=0, strict=True)
    title: str | None = None
    content: str | None = None


def build_router(admin_auth: Any) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    def store(request: Any) -> Any:
        return request.app.state.store

    def translate(exc: Exception) -> HTTPException:
        if exc.__class__.__name__ == "RevisionConflict":
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, (ValueError, TypeError)):
            return HTTPException(status_code=422, detail=str(exc))
        raise exc

    @router.get("/entities")
    async def entities(request: Request, kind: str | None = None, q: str | None = None,
                       limit: int = Query(50, ge=1), offset: int = Query(0, ge=0),
                       competition: str | None = None, year: str | None = None,
                       category: str | None = None, vulnerability_type: str | None = None,
                       verification_status: str | None = None, source: str | None = None) -> dict[str, Any]:
        filters = {k: v for k, v in locals().copy().items() if k in {"competition", "year", "category", "vulnerability_type", "verification_status", "source"} and v is not None}
        items = await asyncio.to_thread(store(request).list_entities, kind=kind, q=q, limit=limit, offset=offset, filters=filters)
        return {"items": items, "limit": limit, "offset": offset}

    @router.post("/entities", dependencies=[Depends(admin_auth)])
    async def create_entity(request: Request, body: EntityBody) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(store(request).create_entity, body.model_dump())
        except Exception as exc:
            raise translate(exc) from exc

    @router.get("/entities/{entity_id}")
    async def get_entity(request: Request, entity_id: str) -> dict[str, Any]:
        item = await asyncio.to_thread(store(request).get_entity, entity_id)
        if item is None:
            raise HTTPException(404, "Entity not found")
        return item

    @router.patch("/entities/{entity_id}", dependencies=[Depends(admin_auth)])
    async def edit_entity(request: Request, entity_id: str, body: EntityPatch) -> dict[str, Any]:
        payload = body.model_dump(exclude={"expected_revision"}, exclude_unset=True)
        try:
            result = await asyncio.to_thread(store(request).update_entity, entity_id, payload, expected_revision=body.expected_revision)
        except Exception as exc:
            raise translate(exc) from exc
        if result is None:
            raise HTTPException(404, "Entity not found")
        return result

    @router.get("/challenges")
    async def challenges(request: Request, q: str | None = None, limit: int = Query(50, ge=1), offset: int = Query(0, ge=0)) -> dict[str, Any]:
        return {"items": await asyncio.to_thread(store(request).list_entities, kind="challenge", q=q, limit=limit, offset=offset)}

    @router.get("/challenges/{entity_id}")
    async def challenge(request: Request, entity_id: str) -> dict[str, Any]:
        item = await asyncio.to_thread(store(request).get_entity, entity_id)
        if item is None or item.get("kind") != "challenge":
            raise HTTPException(404, "Challenge not found")
        return {**item, "graph": await asyncio.to_thread(store(request).related, entity_id)}

    @router.get("/relations")
    async def relations(request: Request, entity_id: str | None = None, kind: str | None = None,
                        status: str | None = None, limit: int = Query(50, ge=1), offset: int = Query(0, ge=0)) -> dict[str, Any]:
        items = await asyncio.to_thread(store(request).list_relations, entity_id=entity_id, kind=kind, status=status, limit=limit, offset=offset)
        return {"items": items, "limit": limit, "offset": offset}

    @router.post("/relations", dependencies=[Depends(admin_auth)])
    async def create_relation(request: Request, body: RelationBody) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(store(request).create_relation, body.model_dump())
        except Exception as exc:
            raise translate(exc) from exc

    @router.get("/relations/{relation_id}")
    async def get_relation(request: Request, relation_id: str) -> dict[str, Any]:
        item = await asyncio.to_thread(store(request).get_relation, relation_id)
        if item is None:
            raise HTTPException(404, "Relation not found")
        return item

    @router.patch("/relations/{relation_id}", dependencies=[Depends(admin_auth)])
    async def edit_relation(request: Request, relation_id: str, body: RelationPatch) -> dict[str, Any]:
        payload = body.model_dump(exclude={"expected_revision"}, exclude_unset=True)
        try:
            item = await asyncio.to_thread(store(request).update_relation, relation_id, payload, expected_revision=body.expected_revision)
        except Exception as exc:
            raise translate(exc) from exc
        if item is None:
            raise HTTPException(404, "Relation not found")
        return item

    @router.get("/entities/{entity_id}/related")
    async def related(request: Request, entity_id: str, depth: int = Query(1, ge=0), limit: int = Query(50, ge=1)) -> dict[str, Any]:
        if await asyncio.to_thread(store(request).get_entity, entity_id) is None:
            raise HTTPException(404, "Entity not found")
        return await asyncio.to_thread(store(request).related, entity_id, depth=depth, limit=limit)

    @router.get("/attack-fix")
    async def attack_fix(request: Request, q: str = "", entity_id: str | None = None, limit: int = Query(20, ge=1)) -> dict[str, Any]:
        return await asyncio.to_thread(store(request).find_attack_fix, q, entity_id=entity_id, limit=limit)

    @router.post("/agent/chat", dependencies=[Depends(admin_auth)])
    async def agent_chat(request: Request, body: ChatBody) -> dict[str, Any]:
        service = getattr(request.app.state, "agent_service", None)
        if service is None:
            raise HTTPException(503, "Agent service is unavailable")
        try:
            return await service.answer(body.question, entity_id=body.entity_id, model=body.model)
        except Exception as exc:
            if exc.__class__.__name__ == "ModelNotFound":
                raise HTTPException(404, str(exc)) from exc
            if exc.__class__.__name__ == "LLMTimeout":
                raise HTTPException(504, str(exc)) from exc
            if exc.__class__.__name__ == "LLMUnavailable":
                raise HTTPException(503, str(exc)) from exc
            raise HTTPException(502, f"Agent request failed: {type(exc).__name__}: {exc}") from exc

    @router.get("/reviews/pending")
    async def pending_reviews(request: Request, limit: int = Query(50, ge=1), offset: int = Query(0, ge=0)) -> dict[str, Any]:
        return {"items": await asyncio.to_thread(store(request).pending_reviews, limit=limit, offset=offset)}

    @router.get("/reviews")
    async def reviews(request: Request, limit: int = Query(100, ge=1), offset: int = Query(0, ge=0)) -> dict[str, Any]:
        return {"items": await asyncio.to_thread(store(request).list_reviews, limit=limit, offset=offset)}

    @router.post("/reviews", dependencies=[Depends(admin_auth)])
    async def submit_review(request: Request, body: ReviewBody) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(store(request).review_record, body.target_type, body.target_id, body.decision,
                                             note=body.note, reviewer=body.reviewer, expected_revision=body.expected_revision)
        except Exception as exc:
            raise translate(exc) from exc
        if result is None:
            raise HTTPException(404, "Review target not found")
        return result

    @router.patch("/knowledge/{knowledge_id}", dependencies=[Depends(admin_auth)])
    async def edit_knowledge(request: Request, knowledge_id: str, body: KnowledgePatch) -> dict[str, Any]:
        payload = body.model_dump(exclude={"expected_revision"}, exclude_unset=True)
        try:
            result = await asyncio.to_thread(store(request).update_knowledge, knowledge_id, payload, expected_revision=body.expected_revision)
        except Exception as exc:
            raise translate(exc) from exc
        if result is None:
            raise HTTPException(404, "Knowledge card not found")
        return result

    @router.get("/analytics")
    async def analytics(request: Request) -> dict[str, Any]:
        s = store(request)
        return {**await asyncio.to_thread(s.knowledge_stats), "store": await asyncio.to_thread(s.stats),
                "jobs": await asyncio.to_thread(s.job_stats)}

    return router
