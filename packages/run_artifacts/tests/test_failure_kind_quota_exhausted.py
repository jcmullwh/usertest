from run_artifacts import classify_failure_kind


def test_classify_failure_kind_quota_exhausted_for_agent_quota_exceeded() -> None:
    is_failure, kind = classify_failure_kind(
        status="error",
        error={
            "type": "AgentQuotaExceeded",
            "code": "claude_out_of_extra_usage",
            "provider": "claude",
            "reset_time": {"raw": "Feb 24, 8pm", "timezone": "America/New_York"},
        },
        validation_errors=[],
    )
    assert is_failure is True
    assert kind == "quota_exhausted"


def test_classify_failure_kind_quota_exhausted_for_provider_quota_subtype() -> None:
    is_failure, kind = classify_failure_kind(
        status="error",
        error={
            "type": "AgentExecFailed",
            "subtype": "provider_quota_exceeded",
        },
        validation_errors=[],
    )
    assert is_failure is True
    assert kind == "quota_exhausted"

