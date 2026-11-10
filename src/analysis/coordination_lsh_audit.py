"""Exact audit of MinHash-LSH candidate recall for a fixed text corpus.

The sparse matrix uses int64 so pairwise shingle intersections cannot silently
wrap at 255. This audit estimates retrieval recall only inside the supplied,
eligible corpus; it says nothing about missing posts or platform prevalence.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

import numpy as np

from analyze import shingles


def exact_cross_channel_edges(
    posts: Iterable[dict], *, minimum: float = 0.6, min_chars: int = 80
) -> list[dict[str, object]]:
    """Exhaustively calculate exact 5-character Jaccard edges.

    This uses the same ``shingles`` function as ``analyze.copy_clusters`` and
    ``continuation_audit.exact_edges``. Posts shorter than ``min_chars`` and
    same-channel pairs are excluded by the existing Social graph definition.
    """
    if not 0.0 < minimum <= 1.0:
        raise ValueError("minimum must be in (0, 1]")
    if type(min_chars) is not int or min_chars < 1:
        raise ValueError("min_chars must be a positive integer")

    eligible = [
        post for post in posts
        if isinstance(post.get("text_clean"), str)
        and len(post["text_clean"]) >= min_chars
        and post.get("channel")
        and post.get("url")
    ]
    eligible.sort(key=lambda post: post["url"])
    if len(eligible) < 2:
        return []

    try:
        from scipy.sparse import csr_matrix
    except ImportError as exc:  # pragma: no cover - environment-specific path
        raise RuntimeError("scipy is required for exhaustive sparse Jaccard audit") from exc

    shingle_sets = [shingles(post["text_clean"]) for post in eligible]
    vocabulary: dict[str, int] = {}
    row_indices: list[int] = []
    col_indices: list[int] = []
    for row_index, tokens in enumerate(shingle_sets):
        for token in tokens:
            column_index = vocabulary.setdefault(token, len(vocabulary))
            row_indices.append(row_index)
            col_indices.append(column_index)

    data = np.ones(len(row_indices), dtype=np.int64)
    matrix = csr_matrix(
        (data, (row_indices, col_indices)),
        shape=(len(eligible), len(vocabulary)),
        dtype=np.int64,
    )
    intersections = (matrix @ matrix.T).tocoo()
    sizes = np.asarray([len(tokens) for tokens in shingle_sets], dtype=np.int64)

    rows: list[dict[str, object]] = []
    for i, j, shared in zip(intersections.row, intersections.col, intersections.data):
        i, j, shared = int(i), int(j), int(shared)
        if i >= j or eligible[i]["channel"] == eligible[j]["channel"]:
            continue
        union = int(sizes[i] + sizes[j] - shared)
        score = shared / union if union else 0.0
        if score < minimum:
            continue
        first, second = sorted(
            (eligible[i], eligible[j]),
            key=lambda post: (datetime.fromisoformat(post["date"]), post["url"]),
        )
        lag_minutes = abs(
            (datetime.fromisoformat(second["date"]) - datetime.fromisoformat(first["date"]))
            .total_seconds()
        ) / 60.0
        rows.append({
            "earlier_url": first["url"],
            "later_url": second["url"],
            "earlier_channel": first["channel"],
            "later_channel": second["channel"],
            "earlier_time": first["date"],
            "later_time": second["date"],
            "lag_minutes": lag_minutes,
            "jaccard": score,
        })
    rows.sort(key=lambda row: (row["earlier_url"], row["later_url"]))
    return rows


def summarize_candidate_recall(
    exact_edges: Iterable[dict],
    candidate_pairs: set[tuple[str, str]],
    *,
    thresholds: Iterable[float] = (0.6, 0.75, 0.9),
) -> list[dict[str, object]]:
    """Report exact-edge recall at each threshold for a fixed candidate set."""
    exact = list(exact_edges)
    results = []
    exact_keys_all = {
        (row["earlier_url"], row["later_url"])
        for row in exact
    }
    for threshold in thresholds:
        threshold = float(threshold)
        if not 0.0 < threshold <= 1.0:
            raise ValueError("thresholds must be in (0, 1]")
        selected = [row for row in exact if float(row["jaccard"]) >= threshold]
        selected_keys = {
            (row["earlier_url"], row["later_url"])
            for row in selected
        }
        returned = selected_keys & candidate_pairs
        results.append({
            "jaccard_threshold": threshold,
            "exact_pairs_n": len(selected_keys),
            "returned_pairs_n": len(returned),
            "missed_pairs_n": len(selected_keys - candidate_pairs),
            "candidate_pairs_not_in_exact_universe_n": len(candidate_pairs - exact_keys_all),
            "recall": len(returned) / len(selected_keys) if selected_keys else None,
        })
    return results
