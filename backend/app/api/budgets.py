from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
from app.db.session import get_db_session
from app.models import UsageBudget, User
from app.schemas.budgets import (
    BudgetCreateRequest,
    BudgetResponse,
    BudgetScope,
    BudgetUpdateRequest,
)
from app.services.budgets import (
    BudgetAccessError,
    BudgetConflictError,
    BudgetNotFoundError,
    BudgetService,
)

router = APIRouter(prefix="/budgets", tags=["budgets"])

DB_SESSION_DEP = Depends(get_db_session)


async def get_budget_service(session: AsyncSession = DB_SESSION_DEP) -> BudgetService:
    return BudgetService(session)


BUDGET_SERVICE_DEP = Depends(get_budget_service)


def _to_response(
    budget: UsageBudget, cost: Decimal, tokens: int, status_value: str
) -> BudgetResponse:
    return BudgetResponse(
        id=budget.id,
        scope=budget.scope,
        scope_id=budget.scope_id,
        period=budget.period,
        limit_cost=float(budget.limit_cost) if budget.limit_cost is not None else None,
        limit_tokens=budget.limit_tokens,
        threshold_percent=budget.threshold_percent,
        enforce=budget.enforce,
        created_at=budget.created_at,
        updated_at=budget.updated_at,
        current_cost=float(cost),
        current_tokens=tokens,
        status=status_value,
    )


@router.get("", response_model=list[BudgetResponse])
async def list_budgets(
    scope: Annotated[BudgetScope, Query()],
    scope_id: Annotated[UUID, Query(alias="id")],
    current_user: User = CURRENT_USER_DEP,
    svc: BudgetService = BUDGET_SERVICE_DEP,
) -> list[BudgetResponse]:
    try:
        enriched = await svc.list_for_scope(current_user.id, scope, scope_id)
    except BudgetAccessError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(error)
        ) from None
    return [_to_response(*row) for row in enriched]


@router.post("", response_model=BudgetResponse, status_code=status.HTTP_201_CREATED)
async def create_budget(
    payload: BudgetCreateRequest,
    current_user: User = CURRENT_USER_DEP,
    svc: BudgetService = BUDGET_SERVICE_DEP,
) -> BudgetResponse:
    try:
        budget = await svc.create(
            user_id=current_user.id,
            scope=payload.scope,
            scope_id=payload.scope_id,
            period=payload.period,
            limit_cost=payload.limit_cost,
            limit_tokens=payload.limit_tokens,
            threshold_percent=payload.threshold_percent,
            enforce=payload.enforce,
        )
    except BudgetAccessError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(error)
        ) from None
    except BudgetConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A budget for this scope and period already exists",
        ) from None
    cost, tokens, status_value = await svc.status_for(budget)
    return _to_response(budget, cost, tokens, status_value)


@router.patch("/{budget_id}", response_model=BudgetResponse)
async def update_budget(
    budget_id: UUID,
    payload: BudgetUpdateRequest,
    current_user: User = CURRENT_USER_DEP,
    svc: BudgetService = BUDGET_SERVICE_DEP,
) -> BudgetResponse:
    changes = payload.model_dump(exclude_unset=True)
    try:
        budget = await svc.update(
            user_id=current_user.id, budget_id=budget_id, changes=changes
        )
    except BudgetNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Budget not found"
        ) from None
    except BudgetAccessError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(error)
        ) from None
    except BudgetConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A budget for this scope and period already exists",
        ) from None
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from None
    cost, tokens, status_value = await svc.status_for(budget)
    return _to_response(budget, cost, tokens, status_value)


@router.delete("/{budget_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_budget(
    budget_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    svc: BudgetService = BUDGET_SERVICE_DEP,
) -> None:
    try:
        await svc.delete(user_id=current_user.id, budget_id=budget_id)
    except BudgetNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Budget not found"
        ) from None
    except BudgetAccessError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(error)
        ) from None
