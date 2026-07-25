"""Single registration and dispatch point for causal proof adapters."""

from __future__ import annotations

import re
from collections.abc import Iterable

from backlog_core.causal_proof import validate_causal_proof_receipt

from backlog_miner.proof_adapters.base import (
    ProofAdapter,
    ProofAdapterContext,
    ProofAdapterResult,
)

_ADAPTER_ID_RE = re.compile(r"[a-z][a-z0-9_.-]+")


class ProofAdapterRegistry:
    def __init__(self, adapters: Iterable[ProofAdapter] = ()) -> None:
        self._adapters: dict[str, ProofAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: ProofAdapter) -> None:
        adapter_id = str(getattr(adapter, "adapter_id", ""))
        version = str(getattr(adapter, "adapter_version", ""))
        if _ADAPTER_ID_RE.fullmatch(adapter_id) is None or not version:
            raise ValueError("proof_adapter_identity_invalid")
        if adapter_id in self._adapters:
            raise ValueError(f"proof_adapter_duplicate:{adapter_id}")
        self._adapters[adapter_id] = adapter

    def adapter_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def evaluate(self, context: ProofAdapterContext) -> ProofAdapterResult:
        adapter_id = context.claim.get("adapter_id")
        adapter = self._adapters.get(str(adapter_id))
        if adapter is None:
            return ProofAdapterResult(diagnostics=(f"proof_adapter_unavailable:{adapter_id}",))
        result = adapter.build(context)
        diagnostics = list(result.diagnostics)
        valid_receipts: list[dict[str, object]] = []
        for receipt in result.receipts:
            receipt_errors = validate_causal_proof_receipt(receipt)
            if receipt_errors:
                diagnostics.extend(
                    f"proof_adapter_receipt_invalid:{adapter_id}:{error}"
                    for error in receipt_errors
                )
            else:
                valid_receipts.append(receipt)
        return ProofAdapterResult(
            receipts=tuple(valid_receipts),
            diagnostics=tuple(dict.fromkeys(diagnostics)),
        )


def adapter_conformance_errors(
    adapter: ProofAdapter,
    contexts: Iterable[ProofAdapterContext],
) -> list[str]:
    """Exercise registration and central receipt invariants without method-name knowledge."""
    try:
        registry = ProofAdapterRegistry([adapter])
    except ValueError as exc:
        return [str(exc)]
    errors: list[str] = []
    exercised = False
    for context in contexts:
        if context.claim.get("adapter_id") != adapter.adapter_id:
            continue
        exercised = True
        result = registry.evaluate(context)
        errors.extend(result.diagnostics)
        if not result.receipts:
            errors.append(f"proof_adapter_conformance_no_receipt:{adapter.adapter_id}")
    if not exercised:
        errors.append(f"proof_adapter_conformance_not_exercised:{adapter.adapter_id}")
    return list(dict.fromkeys(errors))
