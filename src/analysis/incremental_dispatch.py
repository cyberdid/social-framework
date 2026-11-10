"""Fail-closed dispatch for a small allowlist of pure downstream analyses.

The dispatcher consumes an explicit invalidation plan plus code-owned adapter
IDs. It does not import names from input data, execute shell commands, fetch
sources, invoke models, or write outputs. Callers remain responsible for
supplying the refreshed typed input payload for each task.
"""
from __future__ import annotations

import hashlib
import heapq
import json
from typing import Any, Callable

from src.analysis.claim_verification import aggregate_verification
from src.analysis.conflict_analysis import analyze_conflict_episode
from src.analysis.event_reconstruction import reconstruct_timeline
from src.analysis.information_dynamics import summarize_information_dynamics
from src.analysis.source_lineage import analyze_source_lineage
from src.analysis.uncertainty_scenarios import analyze_uncertainty_scenarios


class IncrementalDispatchError(ValueError):
    """Malformed dispatch plan or unsafe adapter selection."""


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise IncrementalDispatchError("dispatch input/result is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _object(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be an object")
    return payload


def _only_keys(payload: dict[str, Any], allowed: set[str], label: str) -> None:
    unexpected = sorted(set(payload) - allowed)
    if unexpected:
        raise ValueError(f"{label} contains unsupported fields")


def _claim_verification(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {"claim", "assessments", "as_of_utc", "search_complete",
               "minimum_independent_roots", "supersedes_result_id"}
    _only_keys(payload, allowed, "claim-verification payload")
    if not {"claim", "assessments", "as_of_utc", "search_complete"}.issubset(payload):
        raise ValueError("claim-verification payload is missing required fields")
    return aggregate_verification(
        payload["claim"], payload["assessments"],
        as_of_utc=payload["as_of_utc"], search_complete=payload["search_complete"],
        minimum_independent_roots=payload.get("minimum_independent_roots", 2),
        supersedes_result_id=payload.get("supersedes_result_id"),
    )


def _source_lineage(payload: dict[str, Any]) -> dict[str, Any]:
    _only_keys(payload, {"records"}, "source-lineage payload")
    if "records" not in payload:
        raise ValueError("source-lineage payload requires records")
    return analyze_source_lineage(payload["records"])


def _timeline(payload: dict[str, Any]) -> dict[str, Any]:
    _only_keys(payload, {"event_mentions", "run_context"}, "timeline payload")
    if "event_mentions" not in payload:
        raise ValueError("timeline payload requires event_mentions")
    return reconstruct_timeline(payload["event_mentions"], run_context=payload.get("run_context"))


def _conflict_episode(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {"episode_id", "physical_event_records", "position_records",
               "information_attention_records", "context"}
    _only_keys(payload, allowed, "conflict-analysis payload")
    required = {"episode_id", "physical_event_records", "position_records",
                "information_attention_records"}
    if not required.issubset(payload):
        raise ValueError("conflict-analysis payload is missing required fields")
    return analyze_conflict_episode(
        payload["episode_id"], payload["physical_event_records"], payload["position_records"],
        payload["information_attention_records"], context=payload.get("context"),
    )


def _information_dynamics(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {"posts", "coverage", "source_lineage", "bin_hours", "timezone_name", "top_k"}
    _only_keys(payload, allowed, "information-dynamics payload")
    if "posts" not in payload:
        raise ValueError("information-dynamics payload requires posts")
    return summarize_information_dynamics(
        payload["posts"], coverage=payload.get("coverage"),
        source_lineage=payload.get("source_lineage"),
        bin_hours=payload.get("bin_hours", 6),
        timezone_name=payload.get("timezone_name", "Europe/Kyiv"),
        top_k=tuple(payload.get("top_k", (1, 3, 5, 10))),
    )


def _uncertainty_scenarios(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {"records", "target_label", "target_group", "top_k", "top_k_basis",
               "coverage_bounds", "post_id_key", "channel_key", "label_key", "group_key", "view_key"}
    _only_keys(payload, allowed, "uncertainty-scenarios payload")
    if not {"records", "target_label"}.issubset(payload):
        raise ValueError("uncertainty-scenarios payload is missing required fields")
    parameters = {key: payload[key] for key in allowed - {"records"} if key in payload}
    return analyze_uncertainty_scenarios(payload["records"], **parameters)


# This map is intentionally code-owned. Inputs can select only these fixed,
# deterministic Python callables; they cannot extend the registry or name an
# import, shell command, callback, model, or network operation.
_ADAPTERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "claim_verification.aggregate.v1": _claim_verification,
    "source_lineage.v1": _source_lineage,
    "event_reconstruction.timeline.v1": _timeline,
    "conflict_analysis.episode.v1": _conflict_episode,
    "information_dynamics.summary.v1": _information_dynamics,
    "uncertainty_scenarios.v1": _uncertainty_scenarios,
}


def _strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise IncrementalDispatchError(f"{label} must be an array of non-empty strings")
    if len(value) != len(set(value)):
        raise IncrementalDispatchError(f"{label} must not contain duplicates")
    return value


def _topological_order(nodes: list[str], edges: list[dict[str, Any]]) -> tuple[list[str], dict[str, list[str]]]:
    if not isinstance(edges, list):
        raise IncrementalDispatchError("snapshot dependency_edges must be an array")
    node_set = set(nodes)
    parents: dict[str, set[str]] = {node: set() for node in nodes}
    children: dict[str, set[str]] = {node: set() for node in nodes}
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            raise IncrementalDispatchError(f"dependency edge {index} must be an object")
        source = edge.get("from", edge.get("from_node_id", edge.get("source_id")))
        target = edge.get("to", edge.get("to_node_id", edge.get("target_id")))
        if not isinstance(source, str) or not source or not isinstance(target, str) or not target:
            raise IncrementalDispatchError(f"dependency edge {index} requires explicit node IDs")
        if source in node_set and target in node_set and source != target:
            parents[target].add(source)
            children[source].add(target)
        elif source == target and source in node_set:
            raise IncrementalDispatchError("dependency graph contains a cycle")
    indegree = {node: len(parents[node]) for node in nodes}
    pending = [node for node, degree in indegree.items() if degree == 0]
    heapq.heapify(pending)
    ordered = []
    while pending:
        node = heapq.heappop(pending)
        ordered.append(node)
        for child in sorted(children[node]):
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(pending, child)
    if len(ordered) != len(nodes):
        raise IncrementalDispatchError("dependency graph contains a cycle")
    return ordered, {node: sorted(values) for node, values in parents.items()}


def plan_recomputations(snapshot: dict[str, Any], bindings: list[dict[str, str]]) -> dict[str, Any]:
    """Build a deterministic plan from an incident snapshot and explicit bindings.

    A binding has exactly ``node_id``, ``adapter_id``, and ``input_key``. Tasks
    not covered by a binding are surfaced as not dispatched; descendants whose
    invalidated predecessor lacks a dispatchable task are blocked.
    """
    if not isinstance(snapshot, dict):
        raise IncrementalDispatchError("snapshot must be an object")
    invalidation = snapshot.get("invalidation_plan")
    if not isinstance(invalidation, dict) or invalidation.get("status") != "planned_not_executed":
        raise IncrementalDispatchError("snapshot must contain a planned_not_executed invalidation plan")
    changed = _strings(invalidation.get("changed_node_ids"), "changed_node_ids")
    invalidated = _strings(invalidation.get("invalidated_node_ids"), "invalidated_node_ids")
    ordered, parents = _topological_order(invalidated, snapshot.get("dependency_edges", []))
    if not isinstance(bindings, list):
        raise IncrementalDispatchError("bindings must be an array")
    binding_by_node: dict[str, dict[str, str]] = {}
    for index, binding in enumerate(bindings):
        if not isinstance(binding, dict) or set(binding) != {"node_id", "adapter_id", "input_key"}:
            raise IncrementalDispatchError(f"binding {index} must contain exactly node_id, adapter_id, input_key")
        node_id, adapter_id, input_key = (binding.get(key) for key in ("node_id", "adapter_id", "input_key"))
        if any(not isinstance(item, str) or not item.strip() for item in (node_id, adapter_id, input_key)):
            raise IncrementalDispatchError(f"binding {index} contains an empty identifier")
        if node_id not in invalidated:
            raise IncrementalDispatchError(f"binding node is not invalidated: {node_id}")
        if adapter_id not in _ADAPTERS:
            raise IncrementalDispatchError(f"adapter is not allowlisted: {adapter_id}")
        if node_id in binding_by_node:
            raise IncrementalDispatchError(f"duplicate dispatch binding for node: {node_id}")
        binding_by_node[node_id] = {"node_id": node_id, "adapter_id": adapter_id,
                                    "input_key": input_key}

    tasks = []
    taskable_nodes = set(binding_by_node)
    for node in ordered:
        binding = binding_by_node.get(node)
        dependencies = parents[node]
        if binding is None:
            status, reason = "not_dispatched", "no_allowlisted_binding"
        elif any(parent not in taskable_nodes for parent in dependencies):
            status, reason = "blocked_missing_dependency_binding", "invalidated_predecessor_not_dispatchable"
        else:
            status, reason = "ready", None
        task_material = {"node_id": node, "binding": binding, "dependency_node_ids": dependencies}
        tasks.append({
            "task_id": "dispatch:" + _digest(task_material)[:24],
            "node_id": node,
            "adapter_id": binding["adapter_id"] if binding else None,
            "input_key": binding["input_key"] if binding else None,
            "dependency_node_ids": dependencies,
            "status": status,
            "reason": reason,
        })
    plan = {
        "schema_version": "1.0",
        "incident_id": snapshot.get("incident_id"),
        "snapshot_run_id": snapshot.get("run_id"),
        "changed_node_ids": sorted(changed),
        "invalidated_node_ids": invalidated,
        "tasks": tasks,
        "limitations": [
            "Only the fixed local pure-function adapter registry is executable; arbitrary imports and commands are rejected.",
            "Each task needs an explicitly refreshed typed input payload supplied by the caller.",
            "Outputs do not automatically feed later tasks; input preparation and persistence remain explicit.",
            "Execution proves that software ran on supplied inputs, not that source evidence is true or complete.",
            "No source collection, network access, LLM call, report write, or ledger mutation occurs.",
        ],
    }
    plan["plan_sha256"] = _digest(plan)
    return plan


def execute_recomputations(plan: dict[str, Any], inputs_by_key: dict[str, Any]) -> dict[str, Any]:
    """Run ready fixed adapters in planned order and return hash-bound receipts.

    Task exceptions are reduced to exception type so potentially sensitive input
    values in exception messages are not copied into receipts.
    """
    if not isinstance(plan, dict) or plan.get("schema_version") != "1.0":
        raise IncrementalDispatchError("dispatch plan schema_version must be 1.0")
    plan_payload = dict(plan)
    supplied_plan_sha256 = plan_payload.pop("plan_sha256", None)
    if not isinstance(supplied_plan_sha256, str) or _digest(plan_payload) != supplied_plan_sha256:
        raise IncrementalDispatchError("dispatch plan fingerprint is missing or does not match")
    if not isinstance(inputs_by_key, dict):
        raise IncrementalDispatchError("inputs_by_key must be an object")
    tasks = plan.get("tasks")
    if not isinstance(tasks, list):
        raise IncrementalDispatchError("dispatch plan requires a tasks array")
    completed: dict[str, dict[str, Any]] = {}
    results = []
    for task in tasks:
        if not isinstance(task, dict):
            raise IncrementalDispatchError("dispatch task must be an object")
        status = task.get("status")
        if status != "ready":
            results.append({key: task.get(key) for key in ("task_id", "node_id", "adapter_id", "status", "reason")})
            completed[task.get("node_id")] = {"status": status}
            continue
        parents = task.get("dependency_node_ids")
        if not isinstance(parents, list) or any(not isinstance(parent, str) for parent in parents):
            raise IncrementalDispatchError("dispatch task dependency_node_ids is malformed")
        if any(parent not in completed or completed[parent]["status"] != "complete" for parent in parents):
            results.append({"task_id": task.get("task_id"), "node_id": task.get("node_id"),
                            "adapter_id": task.get("adapter_id"), "status": "blocked_upstream_failure",
                            "reason": "upstream_dispatch_failed"})
            completed[task.get("node_id")] = {"status": "blocked_upstream_failure"}
            continue
        input_key = task.get("input_key")
        if not isinstance(input_key, str) or input_key not in inputs_by_key:
            result = {"task_id": task.get("task_id"), "node_id": task.get("node_id"),
                      "adapter_id": task.get("adapter_id"), "status": "failed",
                      "reason": "input_payload_missing"}
            results.append(result)
            completed[task.get("node_id")] = {"status": "failed"}
            continue
        adapter_id = task.get("adapter_id")
        adapter = _ADAPTERS.get(adapter_id)
        if adapter is None:
            raise IncrementalDispatchError(f"dispatch plan adapter is not allowlisted: {adapter_id}")
        input_sha256 = None
        try:
            payload = _object(inputs_by_key[input_key], "task input payload")
            input_sha256 = _digest(payload)
            value = adapter(payload)
            output_sha256 = _digest(value)
        except Exception as exc:
            result = {"task_id": task.get("task_id"), "node_id": task.get("node_id"),
                      "adapter_id": adapter_id, "status": "failed",
                      "reason": "invalid_input" if isinstance(exc, (ValueError, TypeError)) else "adapter_error",
                      "exception_type": type(exc).__name__}
            if input_sha256 is not None:
                result["input_sha256"] = input_sha256
            results.append(result)
            completed[task.get("node_id")] = {"status": "failed"}
            continue
        result = {"task_id": task.get("task_id"), "node_id": task.get("node_id"),
                  "adapter_id": adapter_id, "status": "complete", "input_sha256": input_sha256,
                  "output_sha256": output_sha256, "result": value}
        results.append(result)
        completed[task.get("node_id")] = {"status": "complete", "result": value,
                                           "output_sha256": output_sha256}
    return {
        "schema_version": "1.0",
        "incident_id": plan.get("incident_id"),
        "snapshot_run_id": plan.get("snapshot_run_id"),
        "tasks": results,
        "limitations": list(plan.get("limitations") or []),
    }
