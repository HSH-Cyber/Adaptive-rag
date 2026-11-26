"""Each TestClient owns a different loop; reset SSE's process-global exit event."""

import pytest
from sse_starlette.sse import AppStatus


@pytest.fixture(autouse=True)
def reset_sse_exit_event():
    AppStatus.should_exit_event = None
    yield
    AppStatus.should_exit_event = None
