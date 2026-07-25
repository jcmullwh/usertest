"""Compatibility adapters for existing source-inspected Python and pytest proof routes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from backlog_core.causal_proof import canonical_json_sha256, proof_receipt_id_for

from backlog_miner.proof_adapters.base import (
    ProofAdapterContext,
    ProofAdapterResult,
    runner_edge,
    runner_node,
)
from backlog_miner.proof_adapters.structured import StructuredReplayAdapter


def _material(context: ProofAdapterContext, service_name: str) -> Mapping[str, Any] | None:
    service = context.services.get(service_name)
    if service is None:
        return None
    value = service(context)
    return value if isinstance(value, Mapping) else None


def _apply_material(
    result: ProofAdapterResult,
    *,
    material: Mapping[str, Any],
    adapter_id: str,
) -> ProofAdapterResult:
    if not result.receipts:
        return result
    receipt = dict(result.receipts[0])
    old_graph = receipt.get("mechanism_graph")
    old_nodes = old_graph.get("nodes", []) if isinstance(old_graph, Mapping) else []
    source_node = old_nodes[0] if old_nodes else None
    outcome_node = old_nodes[-1] if old_nodes else None
    material_nodes_raw = material.get("nodes")
    material_edges_raw = material.get("edges")
    material_nodes = material_nodes_raw if isinstance(material_nodes_raw, list) else []
    material_edges = material_edges_raw if isinstance(material_edges_raw, list) else []
    if (
        not isinstance(source_node, dict)
        or not isinstance(outcome_node, dict)
        or not material_nodes
    ):
        return ProofAdapterResult(diagnostics=(f"{adapter_id}_material_incomplete",))
    nodes = [source_node, *material_nodes, outcome_node]
    first = material_nodes[0]
    last = material_nodes[-1]
    edges = [
        runner_edge(
            source=source_node["node_id"],
            target=first["node_id"],
            kind="binds_inspected_mechanism",
            evidence=material,
        ),
        *material_edges,
        runner_edge(
            source=last["node_id"],
            target=outcome_node["node_id"],
            kind="produces_observable",
            evidence=receipt["observations"]["challenge"],
        ),
    ]
    receipt["mechanism_graph"] = {
        "root_node_id": source_node["node_id"],
        "outcome_node_id": outcome_node["node_id"],
        "nodes": nodes,
        "edges": edges,
    }
    receipt["adapter_evidence"] = {
        **receipt.get("adapter_evidence", {}),
        "legacy_runner_material": material.get("receipt"),
        "legacy_runner_material_sha256": canonical_json_sha256(material),
    }
    receipt["proof_receipt_id"] = proof_receipt_id_for(receipt)
    return ProofAdapterResult(receipts=(receipt,))


class PythonCallChainProofAdapter(StructuredReplayAdapter):
    adapter_id = "python_call_chain.v1"
    mechanism_kind = "function"

    def build(self, context: ProofAdapterContext) -> ProofAdapterResult:
        material = _material(context, "python_call_chain")
        if material is None:
            return ProofAdapterResult(diagnostics=("python_call_chain_unattested",))
        return _apply_material(
            super().build(context),
            material=material,
            adapter_id=self.adapter_id,
        )


class PytestControlledDifferenceProofAdapter(StructuredReplayAdapter):
    adapter_id = "pytest_controlled_difference.v1"
    mechanism_kind = "function"

    def build(self, context: ProofAdapterContext) -> ProofAdapterResult:
        material = _material(context, "pytest_controlled_difference")
        if material is None:
            return ProofAdapterResult(diagnostics=("pytest_controlled_difference_unattested",))
        return _apply_material(
            super().build(context),
            material=material,
            adapter_id=self.adapter_id,
        )


def material_nodes_from_symbols(
    symbols: list[tuple[str, str, str]],
    edges: list[tuple[str, str, str]],
) -> dict[str, Any]:
    """Convert already runner-inspected symbols/edges into generic adapter material."""
    nodes = [
        runner_node(
            node_id=f"proof:symbol:{index}",
            kind="function",
            locator=f"{path}:{symbol}",
            evidence={"path": path, "symbol": symbol, "source_sha256": source_sha256},
        )
        for index, (symbol, path, source_sha256) in enumerate(symbols)
    ]
    index_by_symbol = {symbol: index for index, (symbol, _path, _sha) in enumerate(symbols)}
    runner_edges = [
        runner_edge(
            source=f"proof:symbol:{index_by_symbol[source]}",
            target=f"proof:symbol:{index_by_symbol[target]}",
            kind="calls",
            evidence={"source": source, "target": target, "edge_sha256": edge_sha256},
        )
        for source, target, edge_sha256 in edges
        if source in index_by_symbol and target in index_by_symbol
    ]
    return {"nodes": nodes, "edges": runner_edges}
