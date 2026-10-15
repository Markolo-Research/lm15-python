"""Reconstruct deserialized auth stream errors without inventing lifecycle certainty."""
import asyncio

import pytest

from lm15.errors import AuthOperationError
from lm15.result import materialize_response, amaterialize_response
from lm15.types import ErrorDetail, StreamErrorEvent


@pytest.mark.parametrize("asynchronous", [False, True])
def test_auth_stream_error_has_conservative_lifecycle(asynchronous):
    event = StreamErrorEvent(error=ErrorDetail(code="auth_operation", message="auth failed"))
    async def events():
        yield event
    with pytest.raises(AuthOperationError) as caught:
        if asynchronous:
            asyncio.run(amaterialize_response(events()))
        else:
            materialize_response(iter([event]))
    assert caught.value.reason == "indeterminate"
    assert caught.value.commit_state == "unknown"
    assert caught.value.recovery == "none"
