from __future__ import annotations

import uuid
from pathlib import Path

from ctfka.contracts import FetchedDocument, ParsedDocument


def seed_awdp_fixture(store) -> dict:
    """Explicitly ingest and graph the bundled fictional AWDP example."""
    source = "fixture-awdp"
    store.register_source(source, "fixture", {"fixture": True})
    path = Path(__file__).with_name("data") / "challenge.md"
    raw = path.read_bytes()
    content = raw.decode("utf-8")
    article = store.ingest(source, FetchedDocument(url="https://fixture.invalid/awdp-2025-cache", raw=raw, metadata={"identity": "awdp-2025-cache", "fixture": True}), ParsedDocument(title="AWDP 2025 Web Cache Challenge", content=content, metadata={"fixture": True}))
    aid, version = article["id"], article["version"]

    def ev(quote: str) -> list[dict]:
        start = content.index(quote)
        return [{"article_id": aid, "version": version, "start": start, "end": start + len(quote), "quote": quote}]

    quotes = {
        "challenge": "The challenge awards a flag when a participant can make the preview service read a file outside its workspace.",
        "north": "Writeup North observed that encoded dot segments survive the first normalization pass and reach the file reader. It links this path traversal attack to Fix A: canonicalize once, resolve symlinks, and enforce a workspace-root boundary after resolution.",
        "south": "Writeup South reproduced traversal with a redirect to an internal file URL. It links this path traversal attack to Fix B: disable redirects for previews and allow only workspace-relative paths.",
        "root": "The root cause is that authorization checks the unnormalized input while the file reader uses a normalized destination.",
        "vulnerability": "A path traversal attack can read `/etc/hostname`; the exploit requires preview access and redirect following.",
        "attack": "A path traversal attack can read `/etc/hostname`; the exploit requires preview access and redirect following.",
        "exploit": "The preview endpoint accepts a URL path and follows redirects before checking the final resolved path.",
        "fix_a": "Fix A resolves the final path and rejects any destination outside the workspace.",
        "patch_a": "Patch A adds a post-resolution root-containment check.",
        "verify_a": "Verification runs encoded traversal and symlink escape cases and confirms both are denied.",
        "fix_b": "Fix B disables redirects and restricts previews to workspace-relative paths.",
        "patch_b": "Patch B rejects redirect responses and absolute paths.",
        "verify_b": "Verification confirms a normal workspace file still previews while redirect and traversal attempts fail.",
    }
    entities = {}
    definitions = [
        ("challenge", "AWDP 2025 Web Cache Challenge", "challenge", {"competition": "AWDP", "year": 2025, "category": "web"}),
        ("north", "Writeup North", "writeup", {"competition": "AWDP", "year": 2025}),
        ("south", "Writeup South", "writeup", {"competition": "AWDP", "year": 2025}),
        ("root", "Authorization and resolution mismatch", "root_cause", {"vulnerability_type": "path_traversal"}),
        ("vulnerability", "Path traversal in preview endpoint", "vulnerability", {"vulnerability_type": "path_traversal"}),
        ("attack", "Redirect-assisted path traversal", "attack", {"vulnerability_type": "path_traversal"}),
        ("exploit", "Read /etc/hostname through preview", "exploit", {}),
        ("fix_a", "Resolve and enforce workspace boundary", "fix", {"tradeoff": "symlink compatibility"}),
        ("patch_a", "Post-resolution containment check", "patch", {}),
        ("verify_a", "Encoded traversal and symlink tests", "verification", {}),
        ("fix_b", "Disable redirects and absolute paths", "fix", {"tradeoff": "remote preview unavailable"}),
        ("patch_b", "Reject redirects and absolute paths", "patch", {}),
        ("verify_b", "Workspace success and traversal denial checks", "verification", {}),
    ]
    for key,title,kind,attrs in definitions:
        ident=uuid.uuid5(uuid.NAMESPACE_URL, f"ctfka-fixture-awdp-v1:{key}").hex
        entities[key]=store.get_entity(ident) or store.create_entity({"id":ident,"kind":kind,"title":title,"description":"Bundled fictional AWDP fixture record.","attributes":attrs,"evidence":ev(quotes[key]),"provenance":"source_claimed"})
    relations=[]
    def link(a,b,kind,quote,rationale=""):
        relations.append(store.create_relation({"source_id":entities[a]["id"],"target_id":entities[b]["id"],"kind":kind,"rationale":rationale,"evidence":ev(quote),"provenance":"source_claimed"}))
    link("challenge","north","challenge_writeup",quotes["north"])
    link("challenge","south","challenge_writeup",quotes["south"])
    link("north","attack","writeup_attack",quotes["north"],"North reproduces encoded-segment path traversal")
    link("south","attack","writeup_attack",quotes["south"],"South reproduces redirect-assisted path traversal")
    link("north","fix_a","writeup_fix",quotes["north"],"North recommends the comprehensive containment fix")
    link("south","fix_b","writeup_fix",quotes["south"],"South recommends disabling redirects")
    link("challenge","vulnerability","challenge_vulnerability",quotes["vulnerability"],"Challenge path traversal vulnerability")
    link("vulnerability","root","vulnerability_root_cause",quotes["root"])
    link("vulnerability","attack","vulnerability_attack",quotes["attack"])
    link("attack","exploit","attack_exploit",quotes["exploit"])
    link("attack","fix_a","attack_fix",quotes["fix_a"],"Comprehensive containment; symlink compatibility cost")
    link("attack","fix_b","attack_fix",quotes["fix_b"],"Smaller change; removes remote preview")
    link("vulnerability","fix_a","vulnerability_fix",quotes["fix_a"])
    link("vulnerability","fix_b","vulnerability_fix",quotes["fix_b"])
    link("fix_a","patch_a","fix_patch",quotes["patch_a"])
    link("fix_b","patch_b","fix_patch",quotes["patch_b"])
    link("fix_a","verify_a","fix_verification",quotes["verify_a"])
    link("fix_b","verify_b","fix_verification",quotes["verify_b"])
    return {"article":article,"article_id":article["id"],"entities":{k:v["id"] for k,v in entities.items()},"relations":[r["id"] for r in relations],"fixture":True}
