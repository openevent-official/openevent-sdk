from __future__ import annotations

from openevent.sdk import AdminClient, OpenEventClient
from openevent.sdk.admin_client import AdminClient as ModuleAdminClient
from openevent.sdk.client import OpenEventClient as ModuleOpenEventClient
from openevent.sdk.proto import admin_pb2, openevent_pb2


def test_public_imports_and_constants() -> None:
    assert AdminClient
    assert OpenEventClient
    assert AdminClient is ModuleAdminClient
    assert OpenEventClient is ModuleOpenEventClient
    assert openevent_pb2.VISIBILITY_PUBLIC == 0
    assert openevent_pb2.VISIBILITY_PROTECTED == 1
    assert openevent_pb2.VISIBILITY_PRIVATE == 2
    assert openevent_pb2.CHANNEL_FILTER_ALL == 0
    event = openevent_pb2.EventMessage(seq=1, channel_id=2, payload=b"ok")
    request = openevent_pb2.FetchRequest(channels=[2])
    response = openevent_pb2.FetchResponse(messages=[event], next_seq=2, last_seq=1)
    admin_request = admin_pb2.ListMessagesRequest(from_seq=1, limit=10)
    admin_response = admin_pb2.ListMessagesResponse(messages=[event], next_seq=2, last_seq=1)
    token_request = admin_pb2.ListTokensRequest(page_token="cursor", limit=10)
    token_response = admin_pb2.ListTokensResponse(next_page_token="next")
    assert event.seq == 1
    assert list(request.channels) == [2]
    assert response.messages[0].payload == b"ok"
    assert response.last_seq == 1
    assert admin_request.limit == 10
    assert admin_response.messages[0].seq == 1
    assert token_request.page_token == "cursor"
    assert token_request.limit == 10
    assert token_response.next_page_token == "next"

    captured = []

    class Stub:
        def ListTokens(self, request):
            captured.append(request)
            return admin_pb2.ListTokensResponse(next_page_token="server-next")

        def ListMessages(self, request):
            captured.append(request)
            return admin_pb2.ListMessagesResponse(next_seq=11, last_seq=10)

    admin_client = AdminClient.__new__(AdminClient)
    admin_client.stub = Stub()
    listed = admin_client.list_tokens(page_token="server-cursor", limit=7)
    assert captured[0].page_token == "server-cursor"
    assert captured[0].limit == 7
    assert listed.next_page_token == "server-next"

    messages = admin_client.list_messages(from_seq=3, limit=8)
    assert captured[1].from_seq == 3
    assert captured[1].limit == 8
    assert messages.next_seq == 11


def test_subscribe_filters_duplicate_and_backward_sequences() -> None:
    class Stream:
        def __init__(self, responses):
            self._responses = iter(responses)
            self.cancelled = False

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._responses)

        def cancel(self):
            self.cancelled = True
            return True

    class Stub:
        def Subscribe(self, request):
            assert request.principal == 7
            self.stream = Stream(
                [
                    openevent_pb2.SubscribeResponse(
                        message=openevent_pb2.EventMessage(seq=2, payload=b"first")
                    ),
                    openevent_pb2.SubscribeResponse(
                        message=openevent_pb2.EventMessage(seq=2, payload=b"duplicate")
                    ),
                    openevent_pb2.SubscribeResponse(
                        message=openevent_pb2.EventMessage(seq=1, payload=b"backward")
                    ),
                    openevent_pb2.SubscribeResponse(
                        message=openevent_pb2.EventMessage(seq=4, payload=b"second")
                    ),
                    openevent_pb2.SubscribeResponse(next_seq=5),
                ]
            )
            return self.stream

    client = OpenEventClient.__new__(OpenEventClient)
    stub = Stub()
    client.event_stub = stub
    responses = list(client.subscribe(principal=7, token="token", from_seq=2))

    assert [response.message.seq for response in responses if response.HasField("message")] == [2, 4]
    assert responses[-1].next_seq == 5
    stream = client.subscribe(principal=7, token="token", from_seq=2)
    assert stream.cancel()
    assert stub.stream.cancelled


def test_proto_service_boundaries_and_admin_field_numbers() -> None:
    assert "AdminService" not in openevent_pb2.DESCRIPTOR.services_by_name
    admin_service = admin_pb2.DESCRIPTOR.services_by_name["AdminService"]
    assert [dependency.name for dependency in openevent_pb2.DESCRIPTOR.dependencies] == []
    assert [dependency.name for dependency in admin_pb2.DESCRIPTOR.dependencies] == ["openevent.proto"]

    list_messages = admin_service.methods_by_name["ListMessages"]
    assert list_messages.input_type.full_name == "openevent.ListMessagesRequest"
    assert list_messages.output_type.full_name == "openevent.ListMessagesResponse"

    list_messages_request = admin_pb2.ListMessagesRequest.DESCRIPTOR.fields_by_name
    assert list_messages_request["from_seq"].number == 1
    assert list_messages_request["limit"].number == 2
    list_messages_response = admin_pb2.ListMessagesResponse.DESCRIPTOR.fields_by_name
    assert list_messages_response["messages"].number == 1
    assert list_messages_response["next_seq"].number == 2
    assert list_messages_response["last_seq"].number == 3

    list_tokens_request = admin_pb2.ListTokensRequest.DESCRIPTOR.fields_by_name
    assert list_tokens_request["page_token"].number == 1
    assert list_tokens_request["limit"].number == 2
    list_tokens_response = admin_pb2.ListTokensResponse.DESCRIPTOR.fields_by_name
    assert list_tokens_response["bindings"].number == 1
    assert list_tokens_response["next_page_token"].number == 2
