"""Reconstruct deserialized auth stream errors without inventing lifecycle certainty."""
import asyncio

import pytest

from lm15.errors import AuthOperationError
from lm15.result import materialize_response, amaterialize_response
from lm15.types import ErrorDetail, StreamErrorEvent, Request, Message


@pytest.mark.parametrize("asynchronous", [False, True])
def test_auth_stream_error_has_conservative_lifecycle(asynchronous):
    request = Request(model="test", messages=(Message.user("test"),))
    event = StreamErrorEvent(error=ErrorDetail(code="auth_operation", message="auth failed"))
    async def events():
        yield event
    with pytest.raises(AuthOperationError) as caught:
        if asynchronous:
            asyncio.run(amaterialize_response(events(), request))
        else:
            materialize_response(iter([event]), request)
    assert caught.value.reason == "indeterminate"
    assert caught.value.commit_state == "unknown"
    assert caught.value.recovery == "none"
