from backlog_miner import (
    load_prompt_manifest,
    run_backlog_ensemble,
    run_backlog_prompt,
    run_labeler_jobs,
)


def test_package_surface_exports_miner_api() -> None:
    assert callable(load_prompt_manifest)
    assert callable(run_backlog_prompt)
    assert callable(run_backlog_ensemble)
    assert callable(run_labeler_jobs)
