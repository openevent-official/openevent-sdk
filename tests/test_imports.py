from __future__ import annotations

from openevent.sdk import AdminClient, OpenEventClient
from openevent.sdk.proto import openevent_pb2


def test_public_imports_and_constants() -> None:
    assert AdminClient
    assert OpenEventClient
    assert openevent_pb2.VISIBILITY_PUBLIC == 0
    assert openevent_pb2.VISIBILITY_PROTECTED == 1
    assert openevent_pb2.VISIBILITY_PRIVATE == 2
    assert openevent_pb2.CHANNEL_FILTER_ALL == 0
    event = openevent_pb2.EventMessage(seq=1, channel_id=2, payload=b"ok")
    request = openevent_pb2.FetchRequest(channels=[2])
    response = openevent_pb2.FetchResponse(messages=[event], next_seq=2, last_seq=1)
    assert event.seq == 1
    assert list(request.channels) == [2]
    assert response.messages[0].payload == b"ok"
    assert response.last_seq == 1
