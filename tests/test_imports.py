from __future__ import annotations

from collections import deque
from threading import Event, Lock

import pytest

from openevent.sdk import AdminClient, OpenEventClient
from openevent.sdk import admin_client as admin_client_module
from openevent.sdk import client as client_module
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
    event = openevent_pb2.EventMessage(seq=1, channel_id=2, payload=b"ok", uuid=7)
    allocate_request = openevent_pb2.AllocateUuidsRequest(count=2)
    allocate_response = openevent_pb2.AllocateUuidsResponse(uuids=[7, 8])
    uuid_lookup_request = openevent_pb2.GetSeqByUuidRequest(uuid=7)
    uuid_lookup_response = openevent_pb2.GetSeqByUuidResponse(seq=1)
    request = openevent_pb2.FetchRequest(channels=[2])
    response = openevent_pb2.FetchResponse(messages=[event], next_seq=2, last_seq=1)
    admin_request = admin_pb2.ListMessagesRequest(from_seq=1, limit=10)
    admin_response = admin_pb2.ListMessagesResponse(messages=[event], next_seq=2, last_seq=1)
    token_request = admin_pb2.ListTokensRequest(page_token="cursor", limit=10)
    token_response = admin_pb2.ListTokensResponse(next_page_token="next")
    assert event.seq == 1
    assert event.uuid == 7
    assert allocate_request.count == 2
    assert list(allocate_response.uuids) == [7, 8]
    assert uuid_lookup_request.uuid == 7
    assert uuid_lookup_response.seq == 1
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
        def ListTokens(self, request, timeout=None):
            captured.append((request, timeout))
            return admin_pb2.ListTokensResponse(next_page_token="server-next")

        def ListMessages(self, request, timeout=None):
            captured.append((request, timeout))
            return admin_pb2.ListMessagesResponse(next_seq=11, last_seq=10)

    admin_client = AdminClient.__new__(AdminClient)
    admin_client.stub = Stub()
    admin_client.timeout = 30.0
    listed = admin_client.list_tokens(page_token="server-cursor", limit=7)
    assert captured[0][0].page_token == "server-cursor"
    assert captured[0][0].limit == 7
    assert captured[0][1] == 30.0
    assert listed.next_page_token == "server-next"

    messages = admin_client.list_messages(from_seq=3, limit=8)
    assert captured[1][0].from_seq == 3
    assert captured[1][0].limit == 8
    assert captured[1][1] == 30.0
    assert messages.next_seq == 11


def test_client_timeout_is_forwarded_to_unary_rpc() -> None:
    class Stub:
        def __init__(self):
            self.timeouts = []

        def GetStatus(self, request, timeout=None):
            self.timeouts.append(timeout)

    client = OpenEventClient.__new__(OpenEventClient)
    stub = Stub()
    client.event_stub = stub
    client.timeout = 1.25
    client.get_status(1, "token")
    assert stub.timeouts == [1.25]


def test_get_uuid_refills_the_local_pool_and_forwards_publish_uuid() -> None:
    class Stub:
        def __init__(self):
            self.allocate_calls = 0

        def AllocateUuids(self, request, timeout=None):
            self.allocate_calls += 1
            assert request.count == 1024
            assert timeout == 1.25
            return openevent_pb2.AllocateUuidsResponse(uuids=range(11, 1035))

        def PublishAutoSeq(self, request, timeout=None):
            assert request.uuid == 12
            assert timeout == 1.25
            return openevent_pb2.PublishAutoSeqResponse(seq=1)

        def GetSeqByUuid(self, request, timeout=None):
            assert request.uuid == 12
            assert timeout == 1.25
            return openevent_pb2.GetSeqByUuidResponse(seq=1)

    client = OpenEventClient.__new__(OpenEventClient)
    stub = Stub()
    client.event_stub = stub
    client._uuid_pool = deque()
    client._uuid_pool_lock = Lock()
    client.timeout = 1.25
    assert client.get_uuid() == 11
    assert client.get_uuid() == 12
    assert stub.allocate_calls == 1
    assert not hasattr(client, "allocate_uuids")
    response = client.publish_auto_seq(1, "token", 2, b"payload", uuid=12)
    assert response.seq == 1
    assert client.get_seq_by_uuid(12) == 1


@pytest.mark.parametrize("client_type", [OpenEventClient, AdminClient])
@pytest.mark.parametrize(
    "timeout",
    [None, True, False, 0, -1, float("inf"), float("-inf"), float("nan")],
)
def test_client_timeout_must_be_finite_and_positive(client_type, timeout) -> None:
    with pytest.raises(ValueError, match="finite number greater than 0"):
        client_type("unused", timeout=timeout)

    client = client_type.__new__(client_type)
    with pytest.raises(ValueError, match="finite number greater than 0"):
        client.timeout = timeout


@pytest.mark.parametrize("client_type", [OpenEventClient, AdminClient])
def test_client_timeout_defaults_to_thirty_seconds(client_type) -> None:
    with client_type("127.0.0.1:1") as client:
        assert client.timeout == 30.0


class _CloseTrackingChannel:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def _patch_stub_constructors(monkeypatch) -> None:
    monkeypatch.setattr(client_module.openevent_pb2_grpc, "EventServiceStub", lambda channel: object())
    monkeypatch.setattr(
        client_module.openevent_pb2_grpc, "ObjectStorageServiceStub", lambda channel: object()
    )
    monkeypatch.setattr(
        client_module.openevent_pb2_grpc, "ChannelServiceStub", lambda channel: object()
    )
    monkeypatch.setattr(admin_client_module.admin_pb2_grpc, "AdminServiceStub", lambda channel: object())


@pytest.mark.parametrize("client_type", [OpenEventClient, AdminClient])
def test_client_closes_owned_channel_once(monkeypatch, client_type) -> None:
    channel = _CloseTrackingChannel()
    monkeypatch.setattr(client_module.grpc, "insecure_channel", lambda target, options: channel)
    _patch_stub_constructors(monkeypatch)

    client = client_type("unused")
    client.close()
    client.close()
    client.__del__()

    assert channel.close_calls == 1


@pytest.mark.parametrize("client_type", [OpenEventClient, AdminClient])
def test_client_does_not_close_injected_channel(monkeypatch, client_type) -> None:
    channel = _CloseTrackingChannel()
    _patch_stub_constructors(monkeypatch)

    client = client_type("unused", channel=channel)
    client.close()
    client.__del__()

    assert channel.close_calls == 0


@pytest.mark.parametrize("client_type", [OpenEventClient, AdminClient])
def test_client_context_manager_closes_owned_channel(monkeypatch, client_type) -> None:
    channel = _CloseTrackingChannel()
    monkeypatch.setattr(client_module.grpc, "insecure_channel", lambda target, options: channel)
    _patch_stub_constructors(monkeypatch)

    with client_type("unused") as client:
        assert client.channel is channel

    assert channel.close_calls == 1


def test_open_event_client_enables_subscribe_keepalive_on_owned_channel(monkeypatch) -> None:
    channel = _CloseTrackingChannel()
    captured_options = []

    def create_channel(target, options):
        assert target == "unused"
        captured_options.extend(options)
        return channel

    monkeypatch.setattr(client_module.grpc, "insecure_channel", create_channel)
    _patch_stub_constructors(monkeypatch)

    client = OpenEventClient("unused")
    client.close()

    assert ("grpc.keepalive_time_ms", 30_000) in captured_options
    assert ("grpc.keepalive_timeout_ms", 10_000) in captured_options
    assert ("grpc.keepalive_permit_without_calls", 0) in captured_options
    assert ("grpc.http2.max_pings_without_data", 0) in captured_options


@pytest.mark.parametrize("client_type", [OpenEventClient, AdminClient])
def test_client_closes_owned_channel_when_stub_construction_fails(
    monkeypatch, client_type
) -> None:
    channel = _CloseTrackingChannel()
    monkeypatch.setattr(client_module.grpc, "insecure_channel", lambda target, options: channel)
    _patch_stub_constructors(monkeypatch)

    def fail(channel):
        raise RuntimeError("stub construction failed")

    if client_type is OpenEventClient:
        monkeypatch.setattr(client_module.openevent_pb2_grpc, "EventServiceStub", fail)
    else:
        monkeypatch.setattr(admin_client_module.admin_pb2_grpc, "AdminServiceStub", fail)

    with pytest.raises(RuntimeError, match="stub construction failed"):
        client_type("unused")

    assert channel.close_calls == 1


@pytest.mark.parametrize("client_type", [OpenEventClient, AdminClient])
def test_client_destructor_tolerates_partial_initialization(client_type) -> None:
    client = client_type.__new__(client_type)
    client.__del__()


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
        def Subscribe(self, request, timeout=None):
            assert request.principal == 7
            assert request.token == "token"
            assert request.from_seq == 2
            assert list(request.channels) == [11, 12]
            assert timeout is None
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
    client.timeout = 2.5
    responses = list(
        client.subscribe(principal=7, token="token", from_seq=2, channels=[11, 12])
    )

    assert [response.message.seq for response in responses if response.HasField("message")] == [2, 4]
    assert responses[-1].next_seq == 5
    stream = client.subscribe(principal=7, token="token", from_seq=2, channels=[11, 12])
    assert stream.cancel()
    assert stub.stream.cancelled


def test_subscribe_wait_started_is_public_and_cancels_on_timeout() -> None:
    class ReadyStream:
        def initial_metadata(self):
            return ()

    ready = client_module._DeduplicatingSubscribeStream(ReadyStream())
    assert ready.wait_started(1) is None

    release = Event()

    class PendingStream:
        def __init__(self):
            self.cancelled = False

        def initial_metadata(self):
            release.wait()
            return ()

        def cancel(self):
            self.cancelled = True
            release.set()
            return True

    pending_stream = PendingStream()
    pending = client_module._DeduplicatingSubscribeStream(pending_stream)
    with pytest.raises(TimeoutError, match="initial metadata"):
        pending.wait_started(0.01)
    assert pending_stream.cancelled
    assert release.wait(1)

    with pytest.raises(ValueError, match="finite number greater than 0"):
        ready.wait_started(0)

    class FailedStream:
        def initial_metadata(self):
            raise RuntimeError("subscription rejected")

    failed = client_module._DeduplicatingSubscribeStream(FailedStream())
    with pytest.raises(RuntimeError, match="subscription rejected"):
        failed.wait_started(1)


def test_proto_service_boundaries_and_admin_field_numbers() -> None:
    assert "AdminService" not in openevent_pb2.DESCRIPTOR.services_by_name
    event_service = openevent_pb2.DESCRIPTOR.services_by_name["EventService"]
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

    subscribe_request = openevent_pb2.SubscribeRequest.DESCRIPTOR.fields_by_name
    assert subscribe_request["channels"].number == 5

    uuid_lookup = event_service.methods_by_name["GetSeqByUuid"]
    assert uuid_lookup.input_type.full_name == "openevent.GetSeqByUuidRequest"
    assert uuid_lookup.output_type.full_name == "openevent.GetSeqByUuidResponse"
    uuid_lookup_request = openevent_pb2.GetSeqByUuidRequest.DESCRIPTOR.fields_by_name
    uuid_lookup_response = openevent_pb2.GetSeqByUuidResponse.DESCRIPTOR.fields_by_name
    assert {name: field.number for name, field in uuid_lookup_request.items()} == {"uuid": 1}
    assert {name: field.number for name, field in uuid_lookup_response.items()} == {"seq": 1}


def test_object_protocol_descriptors_and_reference_order() -> None:
    object_service = openevent_pb2.DESCRIPTOR.services_by_name["ObjectStorageService"]
    assert list(object_service.methods_by_name) == [
        "WriteObject",
        "GetObjectMetadata",
        "ReadObject",
    ]

    object_key = openevent_pb2.ObjectKey.DESCRIPTOR.fields_by_name
    assert object_key["object_id"].number == 1
    assert object_key["object_token"].number == 2

    event_message = openevent_pb2.EventMessage.DESCRIPTOR.fields_by_name
    assert event_message["object_keys"].number == 7
    publish = openevent_pb2.PublishRequest.DESCRIPTOR.fields_by_name
    assert publish["recipients"].number == 5
    assert publish["payload"].number == 6
    assert publish["object_keys"].number == 7
    publish_auto = openevent_pb2.PublishAutoSeqRequest.DESCRIPTOR.fields_by_name
    assert publish_auto["recipients"].number == 4
    assert publish_auto["payload"].number == 5
    assert publish_auto["object_keys"].number == 6

    write = openevent_pb2.WriteObjectRequest.DESCRIPTOR.fields_by_name
    assert {name: field.number for name, field in write.items()} == {
        "principal": 1,
        "token": 2,
        "name": 3,
        "type": 4,
        "description": 5,
        "data": 6,
    }
    write_response = openevent_pb2.WriteObjectResponse.DESCRIPTOR.fields_by_name
    assert write_response["object_id"].number == 1
    assert write_response["object_token"].number == 2
    metadata = openevent_pb2.GetObjectMetadataResponse.DESCRIPTOR.fields_by_name
    assert {name: field.number for name, field in metadata.items()} == {
        "name": 1,
        "type": 2,
        "description": 3,
        "nbytes": 4,
    }
    read = openevent_pb2.ReadObjectRequest.DESCRIPTOR.fields_by_name
    assert {name: field.number for name, field in read.items()} == {
        "object_id": 1,
        "object_token": 2,
        "offset": 3,
        "nbytes": 4,
    }

    first = openevent_pb2.ObjectKey(object_id=1, object_token="first")
    second = openevent_pb2.ObjectKey(object_id=2, object_token="second")
    message = openevent_pb2.EventMessage(object_keys=[first, second, first])
    request = openevent_pb2.PublishAutoSeqRequest(object_keys=[first, second, first])
    assert [key.object_id for key in message.object_keys] == [1, 2, 1]
    assert [key.object_id for key in request.object_keys] == [1, 2, 1]


def test_high_level_object_and_reference_requests_are_complete() -> None:
    event_calls = []
    object_calls = []

    class EventStub:
        def GetSeqByUuid(self, request, timeout=None):
            event_calls.append(("get_seq_by_uuid", request, timeout))
            return openevent_pb2.GetSeqByUuidResponse(seq=9)

        def Publish(self, request, timeout=None):
            event_calls.append(("publish", request, timeout))
            return openevent_pb2.PublishResponse()

        def PublishAutoSeq(self, request, timeout=None):
            event_calls.append(("publish_auto_seq", request, timeout))
            return openevent_pb2.PublishAutoSeqResponse(seq=9)

    class ObjectStub:
        def WriteObject(self, request, timeout=None):
            object_calls.append(("write", request, timeout))
            return openevent_pb2.WriteObjectResponse(object_id=3, object_token="object-token")

        def GetObjectMetadata(self, request, timeout=None):
            object_calls.append(("metadata", request, timeout))
            return openevent_pb2.GetObjectMetadataResponse(nbytes=4)

        def ReadObject(self, request, timeout=None):
            object_calls.append(("read", request, timeout))
            return openevent_pb2.ReadObjectResponse(data=b"data")

    client = OpenEventClient.__new__(OpenEventClient)
    client.event_stub = EventStub()
    client.object_stub = ObjectStub()
    client.timeout = 4.5
    first = openevent_pb2.ObjectKey(object_id=1, object_token="first")
    second = openevent_pb2.ObjectKey(object_id=2, object_token="second")

    assert client.get_seq_by_uuid(18) == 9

    client.publish(
        principal=7,
        token="principal-token",
        channel_id=8,
        seq=9,
        recipients=[10, 11],
        payload=b"publish",
        object_keys=[first, second, first],
        uuid=17,
    )
    client.publish_auto_seq(
        principal=7,
        token="principal-token",
        channel_id=8,
        recipients=[11, 10],
        payload=b"auto",
        object_keys=[second, first],
        uuid=18,
    )
    written = client.write_object(
        principal=7,
        token="principal-token",
        name="name",
        type="application/octet-stream",
        description="description",
        data=b"data",
    )
    metadata = client.get_object_metadata(written.object_id, written.object_token)
    data = client.read_object(written.object_id, written.object_token, offset=1, nbytes=2)

    assert [call[0] for call in event_calls] == ["get_seq_by_uuid", "publish", "publish_auto_seq"]
    assert all(call[2] == 4.5 for call in event_calls)
    assert event_calls[0][1].uuid == 18
    assert list(event_calls[1][1].recipients) == [10, 11]
    assert [key.object_id for key in event_calls[1][1].object_keys] == [1, 2, 1]
    assert event_calls[1][1].uuid == 17
    assert list(event_calls[2][1].recipients) == [11, 10]
    assert [key.object_id for key in event_calls[2][1].object_keys] == [2, 1]
    assert event_calls[2][1].uuid == 18

    assert [call[0] for call in object_calls] == ["write", "metadata", "read"]
    assert all(call[2] == 4.5 for call in object_calls)
    assert object_calls[0][1].principal == 7
    assert object_calls[0][1].token == "principal-token"
    assert object_calls[0][1].name == "name"
    assert object_calls[0][1].type == "application/octet-stream"
    assert object_calls[0][1].description == "description"
    assert object_calls[0][1].data == b"data"
    assert object_calls[1][1].object_id == 3
    assert object_calls[1][1].object_token == "object-token"
    assert object_calls[2][1].offset == 1
    assert object_calls[2][1].nbytes == 2
    assert metadata.nbytes == 4
    assert data.data == b"data"
