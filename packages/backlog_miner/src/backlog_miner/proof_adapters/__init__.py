"""Built-in causal proof adapter registry."""

from backlog_miner.proof_adapters.base import (
    ProofAdapter,
    ProofAdapterContext,
    ProofAdapterResult,
)
from backlog_miner.proof_adapters.basis import (
    AuthenticatedSemanticCitationBasisAdapter,
    OriginExactValueBasisAdapter,
    PositiveBasisAdapter,
    PositiveBasisContext,
    PositiveBasisRegistry,
    PositiveBasisResult,
    RepositoryContractQuoteBasisAdapter,
    RepositoryFailFirstCommandBasisAdapter,
    builtin_positive_basis_registry,
    repository_contract_quote_provenance,
)
from backlog_miner.proof_adapters.legacy import (
    PytestControlledDifferenceProofAdapter,
    PythonCallChainProofAdapter,
)
from backlog_miner.proof_adapters.registry import (
    ProofAdapterRegistry,
    adapter_conformance_errors,
)
from backlog_miner.proof_adapters.structured import (
    CommandTraceProofAdapter,
    ConfigRepositoryStateProofAdapter,
    EnvironmentProofAdapter,
    FilesystemStateProofAdapter,
    PlatformProofAdapter,
    StructuredReplayAdapter,
)


def builtin_proof_adapter_registry() -> ProofAdapterRegistry:
    return ProofAdapterRegistry(
        [
            PythonCallChainProofAdapter(),
            PytestControlledDifferenceProofAdapter(),
            ConfigRepositoryStateProofAdapter(),
            EnvironmentProofAdapter(),
            FilesystemStateProofAdapter(),
            PlatformProofAdapter(),
            CommandTraceProofAdapter(),
            StructuredReplayAdapter(),
        ]
    )


__all__ = [
    "AuthenticatedSemanticCitationBasisAdapter",
    "CommandTraceProofAdapter",
    "ConfigRepositoryStateProofAdapter",
    "EnvironmentProofAdapter",
    "FilesystemStateProofAdapter",
    "OriginExactValueBasisAdapter",
    "PlatformProofAdapter",
    "PositiveBasisAdapter",
    "PositiveBasisContext",
    "PositiveBasisRegistry",
    "PositiveBasisResult",
    "ProofAdapter",
    "ProofAdapterContext",
    "ProofAdapterRegistry",
    "ProofAdapterResult",
    "PytestControlledDifferenceProofAdapter",
    "PythonCallChainProofAdapter",
    "RepositoryContractQuoteBasisAdapter",
    "RepositoryFailFirstCommandBasisAdapter",
    "StructuredReplayAdapter",
    "adapter_conformance_errors",
    "builtin_proof_adapter_registry",
    "builtin_positive_basis_registry",
    "repository_contract_quote_provenance",
]
