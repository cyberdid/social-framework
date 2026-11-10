"""Claim-scoped evidence dependency candidates and confirmed asset families."""
from __future__ import annotations

import hashlib
import unicodedata
from collections import defaultdict


EXPLICIT_RELATIONS = (
    ("derived_from_evidence_ids", "derived_from"),
    ("forwarded_from_evidence_id", "reposts"),
    ("quoted_evidence_ids", "quotes"),
    ("syndicated_from_evidence_id", "syndicated_from"),
    ("translation_of_evidence_id", "translation_of"),
)


def _edge_id(claim_id: str, relation: str, evidence_ids: list[str], basis: str) -> str:
    material = "\x1f".join([claim_id, relation, *sorted(evidence_ids), basis])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _normalize_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _components(nodes: list[str], edges: list[dict]) -> list[set[str]]:
    parent = {node: node for node in nodes}

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left, right):
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for edge in edges:
        if edge["certainty"] not in ("confirmed_by_explicit_reference", "identical_asset_bytes"):
            continue
        ids = edge.get("evidence_ids") or []
        if len(ids) > 1 and all(eid in parent for eid in ids):
            for other in ids[1:]:
                union(ids[0], other)
    components = defaultdict(set)
    for node in nodes:
        components[find(node)].add(node)
    return list(components.values())


def analyze_source_lineage(records: list[dict]) -> dict:
    """Build scoped provenance edges without treating candidate copies as proof.

    Confirmed edges come only from explicit record references or identical media
    hashes. Exact text is a non-directional possible-dependency signal. The
    number of independent roots remains zero unless independence is established
    outside this routine; an unknown relationship cannot be assumed absent.
    """
    by_id = {}
    by_claim: dict[str, list[dict]] = defaultdict(list)
    for row in records:
        evidence_id = str(row.get("evidence_id") or "").strip()
        claim_id = str(row.get("claim_id") or "").strip()
        if not evidence_id or not claim_id:
            raise ValueError("each evidence record requires evidence_id and claim_id")
        if evidence_id in by_id:
            raise ValueError(f"duplicate evidence_id: {evidence_id}")
        by_id[evidence_id] = row
        by_claim[claim_id].append(row)

    edges = []
    invalid_refs = []
    for row in records:
        child_id = str(row["evidence_id"])
        claim_id = str(row["claim_id"])
        for field, relation in EXPLICIT_RELATIONS:
            refs = row.get(field) or []
            if isinstance(refs, str):
                refs = [refs]
            for parent_id in refs:
                parent_id = str(parent_id)
                parent_row = by_id.get(parent_id)
                if parent_row is None or str(parent_row.get("claim_id")) != claim_id:
                    invalid_refs.append({"evidence_id": child_id, "reference": parent_id,
                                         "field": field, "status": "unresolved_or_cross_claim"})
                    continue
                edge = {
                    "relation": relation, "claim_id": claim_id,
                    "evidence_ids": [parent_id, child_id],
                    "parent_evidence_id": parent_id, "child_evidence_id": child_id,
                    "directional": True, "certainty": "confirmed_by_explicit_reference",
                    "basis": field,
                }
                edge["edge_id"] = _edge_id(claim_id, relation, edge["evidence_ids"], field)
                edges.append(edge)

    for claim_id, rows in by_claim.items():
        media_groups = defaultdict(list)
        exact_text_groups = defaultdict(list)
        for row in rows:
            evidence_id = str(row["evidence_id"])
            media_hash = str(row.get("media_sha256") or "").lower()
            if len(media_hash) == 64 and all(char in "0123456789abcdef" for char in media_hash):
                media_groups[media_hash].append(evidence_id)
            normalized = _normalize_text(str(row.get("text") or ""))
            if normalized:
                exact_text_groups[normalized].append(evidence_id)

        for digest, evidence_ids in media_groups.items():
            unique_ids = sorted(set(evidence_ids))
            for other_id in unique_ids[1:]:
                ids = [unique_ids[0], other_id]
                edge = {"relation": "shared_media_segment", "claim_id": claim_id,
                        "evidence_ids": ids, "directional": False,
                        "certainty": "identical_asset_bytes", "basis": "same_media_sha256",
                        "media_sha256": digest}
                edge["edge_id"] = _edge_id(claim_id, edge["relation"], ids, digest)
                edges.append(edge)

        for normalized, evidence_ids in exact_text_groups.items():
            unique_ids = sorted(set(evidence_ids))
            if len(unique_ids) < 2:
                continue
            confirmed_pairs = {tuple(sorted(edge["evidence_ids"])) for edge in edges
                               if edge.get("claim_id") == claim_id}
            for other_id in unique_ids[1:]:
                ids = [unique_ids[0], other_id]
                if tuple(sorted(ids)) in confirmed_pairs:
                    continue
                edge = {"relation": "possible_dependency", "claim_id": claim_id,
                        "evidence_ids": ids, "directional": False,
                        "certainty": "candidate", "basis": "exact_normalized_text"}
                edge["edge_id"] = _edge_id(claim_id, edge["relation"], ids, normalized)
                edges.append(edge)

    edges.sort(key=lambda edge: (edge["claim_id"], edge["relation"], edge["edge_id"]))
    summaries = {}
    for claim_id, rows in by_claim.items():
        evidence_ids = sorted(str(row["evidence_id"]) for row in rows)
        claim_edges = [edge for edge in edges if edge["claim_id"] == claim_id]
        confirmed = [edge for edge in claim_edges if edge["certainty"] in
                     ("confirmed_by_explicit_reference", "identical_asset_bytes")]
        components = _components(evidence_ids, confirmed)
        linked = {eid for edge in confirmed for eid in edge["evidence_ids"]}
        summaries[claim_id] = {
            "evidence_count": len(evidence_ids),
            "confirmed_dependency_family_count": len(components),
            "confirmed_dependency_families": [sorted(component) for component in sorted(components, key=lambda c: sorted(c))],
            "candidate_dependency_edge_count": sum(edge["certainty"] == "candidate" for edge in claim_edges),
            "unknown_lineage_count": sum(eid not in linked for eid in evidence_ids),
            "independent_roots_lower_bound": 0,
            "limitations": [
                "A dependency family is not a source-of-origin finding.",
                "The earliest observed item is not assumed to be the original source.",
                "No relation found does not establish independence; independent root lower bound remains zero without separate evidence.",
            ],
        }
    return {"schema_version": "1.0", "edges": edges, "claim_summaries": summaries,
            "invalid_references": invalid_refs}
