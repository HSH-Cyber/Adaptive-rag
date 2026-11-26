"""Chat routes: single-shot JSON and SSE streaming."""

from fastapi import APIRouter, Depends, Request
from sse_starlette import JSONServerSentEvent
from sse_starlette.sse import EventSourceResponse

from app.api.dependencies import get_container
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.container import AppContainer

#创建聊天功能的路由（窗口）
router = APIRouter(tags=["chat"])

#当用户向 /chat 发送 POST 请求时，就调用下面这个函数。它是一个装饰器，用来把下面的函数注册为 API 接口
@router.post("/chat", response_model=ChatResponse)
async def chat(
    #接收用户发送过来的数据。其次ChatRequest 是一个数据模型。它规定了用户应该提交什么内容
    payload: ChatRequest,
    #FastAPI 提供的请求对象。
    request: Request,
    #Depends() 是 FastAPI 提供的依赖注入机制，请帮我获取已经准备好的项目组件
    container: AppContainer = Depends(get_container),
) -> ChatResponse:
    """Answer one turn and return the full JSON response."""
    return await container.chat_service.chat(payload, trace_id=request.state.trace_id)


@router.post("/chat/stream")
async def chat_stream(
    payload: ChatRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> EventSourceResponse:
    """Answer one turn as an SSE stream of node, token and answer events."""

    async def events():
        async for item in container.chat_service.stream(payload, trace_id=request.state.trace_id):
            yield JSONServerSentEvent(item)

    return EventSourceResponse(events())
