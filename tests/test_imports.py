from __future__ import annotations

from collections import deque
from threading import Event, Lock, Thread

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

    captured = []

    class Stub:
        def ListMessages(self, request, timeout=None):
            captured.append((request, timeout))
            return admin_pb2.ListMessagesResponse(next_seq=11, last_seq=10)

    admin_client = AdminClient.__new__(AdminClient)
    admin_client.stub = Stub()
    admin_client.timeout_ms = 30_000
    messages = admin_client.list_messages(from_seq=3, limit=8)
    assert captured[0][0].from_seq == 3
    assert captured[0][0].limit == 8
    assert captured[0][1] == 30.0
    assert messages.next_seq == 11


def test_client_timeout_ms_is_converted_to_seconds_for_unary_rpc() -> None:
    class Stub:
        def __init__(self):
            self.timeouts = []

        def GetStatus(self, request, timeout=None):
            self.timeouts.append(timeout)

    client = OpenEventClient.__new__(OpenEventClient)
    stub = Stub()
    client.event_stub = stub
    client.timeout_ms = 1_250
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
    client.timeout_ms = 1_250
    assert client.get_uuid() == 11
    assert client.get_uuid() == 12
    assert stub.allocate_calls == 1
    assert not hasattr(client, "allocate_uuids")
    response = client.publish_auto_seq(1, "token", 2, b"payload", uuid=12)
    assert response.seq == 1
    assert client.get_seq_by_uuid(12) == 1


@pytest.mark.parametrize("client_type", [OpenEventClient, AdminClient])
@pytest.mark.parametrize(
    "timeout_ms",
    [None, True, False, 0, -1, float("inf"), float("-inf"), float("nan")],
)
def test_client_timeout_ms_must_be_finite_and_positive(client_type, timeout_ms) -> None:
    with pytest.raises(ValueError, match="timeout_ms must be a finite number greater than 0"):
        client_type("unused", timeout_ms=timeout_ms)

    client = client_type.__new__(client_type)
    with pytest.raises(ValueError, match="timeout_ms must be a finite number greater than 0"):
        client.timeout_ms = timeout_ms


@pytest.mark.parametrize("client_type", [OpenEventClient, AdminClient])
def test_client_timeout_defaults_to_thirty_thousand_milliseconds(client_type) -> None:
    with client_type("127.0.0.1:1") as client:
        assert client.timeout_ms == 30_000


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


def test_close_cancels_pending_uuid_allocation_before_waiting_for_its_lock(monkeypatch) -> None:
    allocation_started = Event()
    allocation_cancelled = Event()
    channel_closed = Event()
    allocation_errors = []
    cancelled_error = client_module.grpc.RpcError("allocation cancelled")

    class Channel(_CloseTrackingChannel):
        def close(self):
            super().close()
            channel_closed.set()
            allocation_cancelled.set()

    class Stub:
        def AllocateUuids(self, request, timeout):
            allocation_started.set()
            if not allocation_cancelled.wait(5):
                raise TimeoutError("allocation was not cancelled")
            raise cancelled_error

    channel = Channel()
    monkeypatch.setattr(client_module.grpc, "insecure_channel", lambda target, options: channel)
    _patch_stub_constructors(monkeypatch)
    client = OpenEventClient("unused")
    client.event_stub = Stub()

    def allocate():
        try:
            client.get_uuid()
        except Exception as exc:
            allocation_errors.append(exc)

    allocator = Thread(target=allocate)
    closer = Thread(target=client.close)
    allocator.start()
    try:
        assert allocation_started.wait(2)
        closer.start()
        assert channel_closed.wait(2), "close waited for the pending allocation before cancelling it"
        allocator.join(2)
        closer.join(2)
        assert not allocator.is_alive()
        assert not closer.is_alive()
        assert allocation_errors == [cancelled_error]
        assert not client._uuid_pool
        assert channel.close_calls == 1
    finally:
        allocation_cancelled.set()
        allocator.join(5)
        if closer.ident is not None:
            closer.join(5)
        client.close()


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
    assert ("grpc.keepalive_permit_without_calls", 1) in captured_options
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


class _TestSubscribeStream:
    def __init__(self, sequences):
        self._responses = iter(
            openevent_pb2.SubscribeResponse(message=openevent_pb2.EventMessage(seq=seq))
            for seq in sequences
        )
        self.cancelled = False
        self.read_count = 0

    def __iter__(self):
        return self

    def __next__(self):
        self.read_count += 1
        return next(self._responses)

    def cancel(self):
        self.cancelled = True
        return True


def test_subscribe_preserves_increasing_sequences_and_forwards_cancellation() -> None:
    class Stub:
        def Subscribe(self, request, timeout=None):
            assert request.principal == 7
            assert request.token == "token"
            assert request.from_seq == 0
            assert list(request.channels) == [11, 12]
            assert timeout is None
            self.stream = _TestSubscribeStream([0, 2, 4])
            return self.stream

    client = OpenEventClient.__new__(OpenEventClient)
    stub = Stub()
    client.event_stub = stub
    client.timeout_ms = 2_500
    responses = list(
        client.subscribe(principal=7, token="token", from_seq=0, channels=[11, 12])
    )

    assert [response.message.seq for response in responses] == [0, 2, 4]
    assert not stub.stream.cancelled
    stream = client.subscribe(principal=7, token="token", from_seq=0, channels=[11, 12])
    assert next(stream).message.seq == 0
    assert stream.cancel()
    assert stub.stream.cancelled


@pytest.mark.parametrize("previous, received", [(0, 0), (2, 2), (2, 1)])
def test_subscribe_rejects_duplicate_and_backward_sequences(previous, received) -> None:
    underlying = _TestSubscribeStream([previous, received, previous + 1])
    stream = client_module._SubscribeStream(underlying)

    assert next(stream).message.seq == previous
    with pytest.raises(RuntimeError, match=f"previous={previous}, received={received}"):
        next(stream)

    assert underlying.cancelled
    assert underlying.read_count == 2


def test_subscribe_wait_started_is_public_and_cancels_on_timeout() -> None:
    class ReadyStream:
        def initial_metadata(self):
            return ()

    ready = client_module._SubscribeStream(ReadyStream())
    assert ready.wait_started(1_000) is None

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
    pending = client_module._SubscribeStream(pending_stream)
    with pytest.raises(TimeoutError, match="initial metadata"):
        pending.wait_started(10)
    assert pending_stream.cancelled
    assert release.wait(1)

    with pytest.raises(ValueError, match="timeout_ms must be a finite number greater than 0"):
        ready.wait_started(0)

    class FailedStream:
        def initial_metadata(self):
            raise RuntimeError("subscription rejected")

    failed = client_module._SubscribeStream(FailedStream())
    with pytest.raises(RuntimeError, match="subscription rejected"):
        failed.wait_started(1_000)


def test_proto_service_boundaries_and_admin_field_numbers() -> None:
    assert "AdminService" not in openevent_pb2.DESCRIPTOR.services_by_name
    event_service = openevent_pb2.DESCRIPTOR.services_by_name["EventService"]
    admin_service = admin_pb2.DESCRIPTOR.services_by_name["AdminService"]
    assert [dependency.name for dependency in openevent_pb2.DESCRIPTOR.dependencies] == []
    assert [dependency.name for dependency in admin_pb2.DESCRIPTOR.dependencies] == ["openevent.proto"]

    get_status = event_service.methods_by_name["GetStatus"]
    assert get_status.input_type.full_name == "openevent.GetStatusRequest"
    assert get_status.output_type.full_name == "openevent.GetStatusResponse"
    get_status_response = openevent_pb2.GetStatusResponse.DESCRIPTOR.fields_by_name
    assert {name: field.number for name, field in get_status_response.items()} == {"max_seq": 2}
    assert get_status_response["max_seq"].type == get_status_response["max_seq"].TYPE_UINT64

    add_token = admin_service.methods_by_name["AddToken"]
    assert add_token.input_type.full_name == "openevent.AddTokenRequest"
    assert add_token.output_type.full_name == "openevent.AddTokenResponse"
    add_token_request = admin_pb2.AddTokenRequest.DESCRIPTOR.fields_by_name
    assert {name: field.number for name, field in add_token_request.items()} == {"principal": 1}
    assert add_token_request["principal"].type == add_token_request["principal"].TYPE_UINT64
    add_token_response = admin_pb2.AddTokenResponse.DESCRIPTOR.fields_by_name
    assert list(add_token_response) == ["token"]
    assert add_token_response["token"].number == 1
    assert add_token_response["token"].type == add_token_response["token"].TYPE_STRING
    assert "TokenBinding" not in admin_pb2.DESCRIPTOR.message_types_by_name

    delete_token = admin_service.methods_by_name["DeleteToken"]
    assert delete_token.input_type.full_name == "openevent.DeleteTokenRequest"
    assert delete_token.output_type.full_name == "openevent.DeleteTokenResponse"
    delete_token_request = admin_pb2.DeleteTokenRequest.DESCRIPTOR.fields_by_name
    assert {name: field.number for name, field in delete_token_request.items()} == {
        "principal": 1, "token": 2,
    }
    assert delete_token_request["principal"].type == delete_token_request["principal"].TYPE_UINT64
    assert delete_token_request["token"].type == delete_token_request["token"].TYPE_STRING

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

    subscribe_request = openevent_pb2.SubscribeRequest.DESCRIPTOR.fields_by_name
    assert subscribe_request["from_seq"].number == 3
    assert subscribe_request["channels"].number == 5
    assert list(openevent_pb2.SubscribeResponse.DESCRIPTOR.fields_by_name) == ["message"]

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
    client.timeout_ms = 4_500
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


@pytest.mark.parametrize("client_type,method,stub_name,rpc,args,kwargs,response_type", [
    (OpenEventClient, "get_status", "event_stub", "GetStatus", (7, "token"), {}, openevent_pb2.GetStatusResponse),
    (OpenEventClient, "fetch", "event_stub", "Fetch", (7, "token", 0, 2), {"channels": [3, 2, 3], "only_my_recipient": True}, openevent_pb2.FetchResponse),
    (OpenEventClient, "create_channel", "channel_stub", "CreateChannel", (7, "token", "name"), {"members": [9, 8, 9], "protocol": "raw", "description": "description", "visibility": 2}, openevent_pb2.CreateChannelResponse),
    (OpenEventClient, "get_channel", "channel_stub", "GetChannel", (7, "token", 3), {}, openevent_pb2.GetChannelResponse),
    (OpenEventClient, "list_channels", "channel_stub", "ListChannels", (7, "token"), {"filter": 2}, openevent_pb2.ListChannelsResponse),
    (OpenEventClient, "add_member", "channel_stub", "AddMember", (7, "token", 3, 9), {}, openevent_pb2.AddMemberResponse),
    (OpenEventClient, "remove_member", "channel_stub", "RemoveMember", (7, "token", 3, 9), {}, openevent_pb2.RemoveMemberResponse),
    (AdminClient, "add_token", "stub", "AddToken", (7,), {}, admin_pb2.AddTokenResponse),
    (AdminClient, "add_token", "stub", "AddToken", (), {"principal": 7}, admin_pb2.AddTokenResponse),
    (AdminClient, "delete_token", "stub", "DeleteToken", (7, "exact-token"), {}, admin_pb2.DeleteTokenResponse),
    (AdminClient, "delete_token", "stub", "DeleteToken", (), {"principal": 7, "token": "exact-token"}, admin_pb2.DeleteTokenResponse),
])
def test_remaining_wrappers_forward_and_preserve_responses(client_type, method, stub_name, rpc, args, kwargs, response_type):
    import inspect
    from types import SimpleNamespace

    response = response_type()
    captured = []

    def call(request, timeout):
        assert timeout == 1.75
        captured.append(request)
        return response

    client = client_type.__new__(client_type)
    client.timeout_ms = 1_750
    setattr(client, stub_name, SimpleNamespace(**{rpc: call}))
    wrapper = getattr(client, method)
    assert wrapper(*args, **kwargs) is response
    bound = inspect.signature(wrapper).bind(*args, **kwargs)
    bound.apply_defaults()
    for name, expected in bound.arguments.items():
        actual = getattr(captured[0], name)
        assert list(actual) == expected if isinstance(expected, list) else actual == expected


def test_uuid_pool_refills_in_order_and_is_discarded_on_close(monkeypatch):
    _patch_stub_constructors(monkeypatch)
    channel = _CloseTrackingChannel()
    monkeypatch.setattr(client_module.grpc, "insecure_channel", lambda target, options: channel)
    calls = []

    class Stub:
        def AllocateUuids(self, request, timeout):
            start = len(calls) * 1024 + 1
            calls.append(request.count)
            return openevent_pb2.AllocateUuidsResponse(uuids=range(start, start + 1024))

        def GetSeqByUuid(self, request, timeout):
            return openevent_pb2.GetSeqByUuidResponse(seq=0)

    client = OpenEventClient("unused")
    client.event_stub = Stub()
    assert [client.get_uuid() for _ in range(1025)] == list(range(1, 1026))
    assert calls == [1024, 1024]
    assert client.get_seq_by_uuid(0) == 0
    assert client.get_uuid() == 1026
    client.close()
    assert not client._uuid_pool
    assert channel.close_calls == 1
