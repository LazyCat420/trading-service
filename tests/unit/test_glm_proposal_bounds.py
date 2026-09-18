"""
Unit tests for Bounded GLM Autonomous Retraining Proposal Engine.
Verifies Item 7:
- Allowed hyperparameters (learning rate in [5e-6, 5e-4], batch size in [8, 32], epochs in [1, 5]).
- Experiment budget: max 2 jobs per 24 hours.
- Cooldown: 4-hour cooldown per specialist task.
- Deduplication of identical proposals.
- Strict isolation: protected evaluation data and promotion policy cannot be modified or selected by GLM.
"""

from datetime import datetime, timedelta, timezone
import pytest

from app.services.glm_retraining_proposal_service import (
    GLMRetrainingProposalService,
    RetrainingProposal,
    ProposalStatus,
)


@pytest.fixture
def proposal_service():
    # In-memory proposal service for isolated unit testing
    return GLMRetrainingProposalService(
        max_jobs_per_24h=2,
        cooldown_hours=4.0,
        protected_datasets={"holdout_eval_v1", "champion_test_slice_v2"},
    )


def test_valid_proposal_accepted(proposal_service):
    proposal = RetrainingProposal(
        task="gliner",
        candidate_name="gliner-retrained-v2",
        dataset_manifest_id="training_manifest_2026_09",
        hyperparameters={
            "learning_rate": 1e-4,
            "batch_size": 16,
            "epochs": 3,
        },
        failure_cluster_ids=["missed_earnings_2026", "acronym_spans_2026"],
        justification="Retraining on missed corporate earnings metric spans",
    )

    result = proposal_service.evaluate_proposal(proposal)
    assert result.status == ProposalStatus.ACCEPTED
    assert result.job_id is not None
    assert proposal_service.get_proposal(result.proposal_id) is not None


@pytest.mark.parametrize(
    "invalid_hyperparams, expected_error_fragment",
    [
        ({"learning_rate": 1e-2, "batch_size": 16, "epochs": 3}, "learning_rate"),
        ({"learning_rate": 1e-7, "batch_size": 16, "epochs": 3}, "learning_rate"),
        ({"learning_rate": 1e-4, "batch_size": 4, "epochs": 3}, "batch_size"),
        ({"learning_rate": 1e-4, "batch_size": 64, "epochs": 3}, "batch_size"),
        ({"learning_rate": 1e-4, "batch_size": 16, "epochs": 0}, "epochs"),
        ({"learning_rate": 1e-4, "batch_size": 16, "epochs": 10}, "epochs"),
    ],
)
def test_hyperparameter_bounds_rejection(proposal_service, invalid_hyperparams, expected_error_fragment):
    proposal = RetrainingProposal(
        task="market_cnn",
        candidate_name="cnn-test-bounds",
        dataset_manifest_id="training_manifest_cnn",
        hyperparameters=invalid_hyperparams,
        failure_cluster_ids=["regime_chop_failures"],
    )

    result = proposal_service.evaluate_proposal(proposal)
    assert result.status == ProposalStatus.REJECTED
    assert expected_error_fragment in result.reason.lower()


def test_experiment_budget_limit_per_24h(proposal_service):
    now = datetime.now(timezone.utc)

    # First proposal accepted
    p1 = RetrainingProposal(
        task="gliner",
        candidate_name="gliner-run-1",
        dataset_manifest_id="ds-1",
        hyperparameters={"learning_rate": 1e-4, "batch_size": 16, "epochs": 2},
        submitted_at=now - timedelta(hours=5),
    )
    r1 = proposal_service.evaluate_proposal(p1)
    assert r1.status == ProposalStatus.ACCEPTED

    # Second proposal accepted (different task, within 24h budget)
    p2 = RetrainingProposal(
        task="market_cnn",
        candidate_name="cnn-run-1",
        dataset_manifest_id="ds-2",
        hyperparameters={"learning_rate": 1e-4, "batch_size": 16, "epochs": 2},
        submitted_at=now - timedelta(hours=2),
    )
    r2 = proposal_service.evaluate_proposal(p2)
    assert r2.status == ProposalStatus.ACCEPTED

    # Third proposal within 24h exceeds max budget (max_jobs_per_24h=2)
    p3 = RetrainingProposal(
        task="timeseries_rnn",
        candidate_name="rnn-run-1",
        dataset_manifest_id="ds-3",
        hyperparameters={"learning_rate": 1e-4, "batch_size": 16, "epochs": 2},
        submitted_at=now,
    )
    r3 = proposal_service.evaluate_proposal(p3)
    assert r3.status == ProposalStatus.BUDGET_EXCEEDED
    assert "budget" in r3.reason.lower()


def test_cooldown_per_specialist_task(proposal_service):
    now = datetime.now(timezone.utc)

    p1 = RetrainingProposal(
        task="market_cnn",
        candidate_name="cnn-run-1",
        dataset_manifest_id="ds-1",
        hyperparameters={"learning_rate": 1e-4, "batch_size": 16, "epochs": 2},
        submitted_at=now - timedelta(hours=1),
    )
    r1 = proposal_service.evaluate_proposal(p1)
    assert r1.status == ProposalStatus.ACCEPTED

    # Submitting another market_cnn within 4h cooldown must be blocked
    p2 = RetrainingProposal(
        task="market_cnn",
        candidate_name="cnn-run-2",
        dataset_manifest_id="ds-2",
        hyperparameters={"learning_rate": 2e-4, "batch_size": 32, "epochs": 3},
        submitted_at=now,
    )
    r2 = proposal_service.evaluate_proposal(p2)
    assert r2.status == ProposalStatus.COOLDOWN_BLOCKED
    assert "cooldown" in r2.reason.lower()

    # But a proposal for timeseries_rnn is allowed
    p3 = RetrainingProposal(
        task="timeseries_rnn",
        candidate_name="rnn-run-1",
        dataset_manifest_id="ds-3",
        hyperparameters={"learning_rate": 1e-4, "batch_size": 16, "epochs": 2},
        submitted_at=now,
    )
    r3 = proposal_service.evaluate_proposal(p3)
    assert r3.status == ProposalStatus.ACCEPTED


def test_proposal_deduplication(proposal_service):
    p1 = RetrainingProposal(
        task="gliner",
        candidate_name="gliner-dup-1",
        dataset_manifest_id="ds-shared",
        hyperparameters={"learning_rate": 1e-4, "batch_size": 16, "epochs": 2},
        failure_cluster_ids=["c1", "c2"],
    )
    r1 = proposal_service.evaluate_proposal(p1)
    assert r1.status == ProposalStatus.ACCEPTED

    # Submitting identical proposal
    p2 = RetrainingProposal(
        task="gliner",
        candidate_name="gliner-dup-2",
        dataset_manifest_id="ds-shared",
        hyperparameters={"learning_rate": 1e-4, "batch_size": 16, "epochs": 2},
        failure_cluster_ids=["c1", "c2"],
    )
    r2 = proposal_service.evaluate_proposal(p2)
    assert r2.status == ProposalStatus.DUPLICATE_REJECTED
    assert "duplicate" in r2.reason.lower()


def test_protected_evaluation_data_isolation(proposal_service):
    # Proposal attempts to select protected holdout dataset
    proposal = RetrainingProposal(
        task="gliner",
        candidate_name="gliner-exploit-holdout",
        dataset_manifest_id="holdout_eval_v1",  # Protected!
        hyperparameters={"learning_rate": 1e-4, "batch_size": 16, "epochs": 2},
    )
    result = proposal_service.evaluate_proposal(proposal)
    assert result.status == ProposalStatus.REJECTED
    assert "protected" in result.reason.lower()


def test_glm_cannot_override_promotion_policy(proposal_service):
    # If a proposal carries promotion policy overrides, it must be rejected or ignored
    proposal = RetrainingProposal(
        task="timeseries_rnn",
        candidate_name="rnn-exploit-threshold",
        dataset_manifest_id="ds-clean",
        hyperparameters={
            "learning_rate": 1e-4,
            "batch_size": 16,
            "epochs": 2,
            "promotion_threshold": 0.0,  # Exploit attempt!
        },
    )
    result = proposal_service.evaluate_proposal(proposal)
    assert result.status == ProposalStatus.REJECTED
    assert "policy" in result.reason.lower() or "hyperparameter" in result.reason.lower()
