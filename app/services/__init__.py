"""Application services, lazily exported to keep providers independently importable."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.chat_service import ChatService as ChatService
    from app.services.container import (
        AppContainer as AppContainer,
        build_container as build_container,
    )

__all__ = ["AppContainer", "ChatService", "build_container"]


def __getattr__(name: str) -> Any:
    if name == "ChatService":
        from app.services.chat_service import ChatService

        return ChatService
    if name in {"AppContainer", "build_container"}:
        from app.services import container

        return getattr(container, name)
    raise AttributeError(name)
