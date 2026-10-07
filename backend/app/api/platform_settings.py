"""Platform-wide LLM settings -- the Global Settings page's backend.

Owner-only for reads and writes, because the singleton row holds a shared credential. The one
exception is ``/status``, which any authenticated user may read so the frontend's first-run gate
can tell whether setup is still pending without leaking the endpoint or the key.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.audit_support import AUDIT_LOG_SERVICE_DEP, record_audit_safely
from app.api.auth import CURRENT_USER_DEP, require_role
from app.core.config import Settings, get_settings
from app.db.session import get_db_session
from app.models import User
from app.schemas.platform_settings import (
    LlmProbeResponse,
    LlmSettingsDeleteResponse,
    LlmSettingsResponse,
    LlmSettingsStatusResponse,
    LlmSettingsTestRequest,
    LlmSettingsUpdateRequest,
)
from app.services.audit import AuditLogService
from app.services.platform_llm import (
    InvalidLlmSettingsError,
    LlmSettingsDecryptionError,
    PlatformLlmSettingsService,
)
from app.services.workspaces import WorkspaceSecretCipher, mask_env_value

router = APIRouter(prefix="/platform/llm-settings", tags=["platform-settings"])

DB_SESSION_DEP = Depends(get_db_session)
SETTINGS_DEP = Depends(get_settings)
# First production use of require_role: the platform owner is the first registered user.
PLATFORM_OWNER_DEP = Depends(require_role("owner"))


async def get_platform_llm_service(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> PlatformLlmSettingsService:
    return PlatformLlmSettingsService(
        session=session,
        cipher=WorkspaceSecretCipher(settings.workspace_env_encryption_key),
        settings=settings,
    )


PLATFORM_LLM_SERVICE_DEP = Depends(get_platform_llm_service)


@router.get("/status", response_model=LlmSettingsStatusResponse)
async def get_llm_settings_status(
    _current_user: User = CURRENT_USER_DEP,
    service: PlatformLlmSettingsService = PLATFORM_LLM_SERVICE_DEP,
) -> LlmSettingsStatusResponse:
    try:
        resolved = await service.resolve()
    except LlmSettingsDecryptionError:
        # The endpoint IS configured; only its key is unreadable. The first-run gate must not
        # trip (that would send everyone to /setup), so report it as configured and let the
        # owner-only GET below deliver the actionable 503.
        row = await service.get_row()
        return LlmSettingsStatusResponse(
            configured=True,
            source="database",
            base_url=row.base_url if row is not None else None,
            model=row.model if row is not None else None,
        )
    if resolved is None:
        return LlmSettingsStatusResponse(configured=False, source=None)
    return LlmSettingsStatusResponse(
        configured=True,
        source=resolved.source,
        base_url=resolved.base_url,
        model=resolved.model,
    )


@router.get("", response_model=LlmSettingsResponse)
async def get_llm_settings(
    _owner: User = PLATFORM_OWNER_DEP,
    service: PlatformLlmSettingsService = PLATFORM_LLM_SERVICE_DEP,
) -> LlmSettingsResponse:
    row = await service.get_row()
    try:
        resolved = await service.resolve()
    except LlmSettingsDecryptionError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from None

    if resolved is None:
        return LlmSettingsResponse()

    # The key is write-only by design: report only that one exists, plus a constant mask.
    has_api_key = resolved.api_key is not None
    return LlmSettingsResponse(
        base_url=resolved.base_url,
        model=resolved.model,
        has_api_key=has_api_key,
        masked_api_key=mask_env_value() if has_api_key else None,
        source=resolved.source,
        updated_at=row.updated_at if row is not None else None,
    )


@router.put("", response_model=LlmSettingsResponse)
async def update_llm_settings(
    payload: LlmSettingsUpdateRequest,
    owner: User = PLATFORM_OWNER_DEP,
    service: PlatformLlmSettingsService = PLATFORM_LLM_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> LlmSettingsResponse:
    try:
        row = await service.upsert(
            base_url=payload.base_url,
            model=payload.model,
            api_key=payload.api_key,
            user_id=owner.id,
        )
    except InvalidLlmSettingsError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from None

    await record_audit_safely(
        audit_log_service,
        user_id=owner.id,
        workspace_id=None,
        action="platform.llm_settings.updated",
        target_type="platform_llm_settings",
        target_id=row.id,
        # `safe_metadata` silently drops any field whose NAME contains
        # token/secret/password/key/credential, so this flag cannot be called
        # `api_key_changed` or `credential_replaced` -- both would vanish from the log.
        metadata={
            "base_url": row.base_url,
            "model": row.model,
            "auth_replaced": payload.api_key is not None,
        },
    )

    has_api_key = row.encrypted_api_key is not None
    return LlmSettingsResponse(
        base_url=row.base_url,
        model=row.model,
        has_api_key=has_api_key,
        masked_api_key=mask_env_value() if has_api_key else None,
        source="database",
        updated_at=row.updated_at,
    )


@router.post("/test", response_model=LlmProbeResponse)
async def test_llm_settings(
    payload: LlmSettingsTestRequest,
    _owner: User = PLATFORM_OWNER_DEP,
    service: PlatformLlmSettingsService = PLATFORM_LLM_SERVICE_DEP,
) -> LlmProbeResponse:
    try:
        resolved = await service.resolve()
    except LlmSettingsDecryptionError:
        # Testing is exactly how an owner recovers from this, so fall back to draft values
        # rather than refusing outright.
        resolved = None

    base_url = (payload.base_url or "").strip() or (resolved.base_url if resolved else "")
    if not base_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Enter a base URL to test.",
        )

    # An omitted key means "test what is already stored", so the owner can validate a saved
    # configuration without retyping a key they cannot read back.
    api_key = (
        payload.api_key if payload.api_key is not None else (resolved.api_key if resolved else None)
    )

    model = (payload.model or "").strip() or (resolved.model if resolved else None)

    try:
        result = await service.probe(base_url, api_key or None, model)
    except InvalidLlmSettingsError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from None

    return LlmProbeResponse(
        ok=result.ok,
        message=result.message,
        models=result.models,
        latency_ms=result.latency_ms,
    )


@router.delete("", response_model=LlmSettingsDeleteResponse)
async def delete_llm_settings(
    owner: User = PLATFORM_OWNER_DEP,
    service: PlatformLlmSettingsService = PLATFORM_LLM_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> LlmSettingsDeleteResponse:
    """Clear the stored configuration so resolution falls back to the environment."""
    cleared = await service.clear()
    if cleared:
        await record_audit_safely(
            audit_log_service,
            user_id=owner.id,
            workspace_id=None,
            action="platform.llm_settings.cleared",
            target_type="platform_llm_settings",
            target_id="singleton",
        )
    return LlmSettingsDeleteResponse(ok=True, cleared=cleared)
