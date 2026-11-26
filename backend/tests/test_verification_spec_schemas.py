"""Plan 3 Task 1: the API carries a workflow's verification criteria and a run's workflow link."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.specs import SpecWorkflowCreateRequest
from app.schemas.verification import VerificationRunResponse


def test_workflow_create_accepts_optional_verification_criteria() -> None:
    plain = SpecWorkflowCreateRequest(feature_name="F", description="d")
    assert plain.verification is None

    req = SpecWorkflowCreateRequest(
        feature_name="F",
        description="d",
        verification={"test_command": "  pytest -q ", "observe_port": 5173, "max_iterations": 3},
    )
    assert req.verification.test_command == "pytest -q"
    assert req.verification.model_dump() == {
        "test_command": "pytest -q",
        "observe_port": 5173,
        "max_iterations": 3,
    }


@pytest.mark.parametrize(
    "verification",
    [
        {"test_command": "   "},
        {"test_command": "pytest", "max_iterations": 0},
        {"test_command": "pytest", "max_iterations": 21},
        {"test_command": "pytest", "observe_port": 70000},
    ],
)
def test_workflow_verification_criteria_are_validated(verification) -> None:
    with pytest.raises(ValidationError):
        SpecWorkflowCreateRequest(feature_name="F", description="d", verification=verification)


def test_run_response_exposes_the_spec_workflow_link() -> None:
    now = datetime.now(UTC)
    workflow_id = uuid4()
    response = VerificationRunResponse(
        id=uuid4(),
        workspace_id=uuid4(),
        plan_ref="specs/f/plan.md",
        status="review",
        spec_workflow_id=workflow_id,
        created_at=now,
        updated_at=now,
    )
    assert response.spec_workflow_id == workflow_id
    assert VerificationRunResponse(
        id=uuid4(), workspace_id=uuid4(), plan_ref="p", status="review",
        created_at=now, updated_at=now,
    ).spec_workflow_id is None
