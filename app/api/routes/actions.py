"""Durable action confirmation and authoritative receipt lookup."""

from fastapi import APIRouter, Depends, Query, Request

from app.api.dependencies import get_container
from app.schemas.actions import ActionConfirmRequest, ActionView
from app.services.container import AppContainer

router = APIRouter(prefix="/actions", tags=["actions"])


@router.get("/{action_id}", response_model=ActionView)
async def get_action(
    action_id: str,
    user_id: str = Query(min_length=1, max_length=64),
    session_id: str = Query(min_length=1, max_length=64),
    container: AppContainer = Depends(get_container),
) -> ActionView:
    """Demo identity binding; only the operation's owner/session can read it."""
    return await container.chat_service.actions.get(
        action_id, user_id=user_id, session_id=session_id
    )


@router.post("/{action_id}/confirm", response_model=ActionView)
async def confirm_action(
    action_id: str,
    payload: ActionConfirmRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> ActionView:
    await container.chat_service._enforce_rate_limit(payload.user_id)
    return await container.chat_service.actions.confirm(
        action_id,
        user_id=payload.user_id,
        session_id=payload.session_id,
        trace_id=request.state.trace_id,
    )
