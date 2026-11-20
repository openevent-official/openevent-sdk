from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import ExitStack
import os
import json
import subprocess
import socket
from threading import Event, Lock, Thread
from typing import Iterator

import grpc
import pytest

from openevent.sdk import AdminClient, OpenEventClient
from openevent.sdk.proto import admin_pb2, admin_pb2_grpc, openevent_pb2, openevent_pb2_grpc


def _free_address():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"127.0.0.1:{sock.getsockname()[1]}"


@pytest.fixture
def server_addresses(tmp_path):
    public = _free_address()
    admin_address = _free_address()
    while admin_address == public:
        admin_address = _free_address()
    config = tmp_path / "server.yaml"
    config.write_text(
        f'grpc:\n  listen_addr: "{public}"\n'
        f'admin:\n  listen_addr: "{admin_address}"\n'
        f'storage:\n  path: {json.dumps(str(tmp_path / "data"), ensure_ascii=False)}\n'
    )
    with (tmp_path / "server.log").open("w") as log:
        process = subprocess.Popen([os.environ["OPENEVENT_SERVER_BIN"], str(config)], stdout=log, stderr=log)
        try:
            yield public, admin_address
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    pytest.fail("server did not shut down after ending subscriptions")
            assert process.returncode == 0


@pytest.fixture
def admin(server_addresses) -> Iterator[AdminClient]:
    _, admin_address = server_addresses
    with AdminClient(admin_address) as value:
        grpc.channel_ready_future(value.channel).result(timeout=10)
        yield value


@pytest.fixture
def client(server_addresses) -> Iterator[OpenEventClient]:
    public, _ = server_addresses
    with OpenEventClient(public) as value:
        grpc.channel_ready_future(value.channel).result(timeout=10)
        yield value


def _token(admin: AdminClient, principal: int) -> str:
    return admin.add_token(principal=principal).token


def _assert_rpc_code(exc_info: pytest.ExceptionInfo[grpc.RpcError], code: grpc.StatusCode) -> None:
    assert exc_info.value.code() == code


def _wait_for_subscription(stream, pending, expected, timeout=5):
    done, _ = wait([pending], timeout=timeout)
    if not done:
        stream.cancel()
        pytest.fail(f"Subscribe timed out after {timeout}s waiting for {expected}")
    return pending.result()


def _next_response(stream, expected, timeout=5):
    with ThreadPoolExecutor(max_workers=1) as executor:
        return _wait_for_subscription(stream, executor.submit(next, stream), expected, timeout)


def _system_event_data(message, channel_id, kind):
    assert message.channel_id == channel_id
    event = json.loads(message.payload)
    assert event["kind"] == kind
    return event["data"]


class _TcpRelay:
    def __init__(self, upstream_target: str):
        host, port = upstream_target.rsplit(":", 1)
        self._upstream = (host, int(port))
        self._stopped = Event()
        self._forwarding = Event()
        self._forwarding.set()
        self._connections_lock = Lock()
        self._connections = []
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self._listener.settimeout(0.1)
        self._accept_thread = Thread(target=self._accept, daemon=True)
        self._accept_thread.start()

    @property
    def target(self) -> str:
        host, port = self._listener.getsockname()
        return f"{host}:{port}"

    def pause(self) -> None:
        self._forwarding.clear()

    def close(self) -> None:
        self._stopped.set()
        self._forwarding.set()
        try:
            self._listener.close()
        except OSError:
            pass
        with self._connections_lock:
            connections = list(self._connections)
        for connection in connections:
            try:
                connection.close()
            except OSError:
                pass
        self._accept_thread.join(timeout=1)

    def _accept(self) -> None:
        while not self._stopped.is_set():
            try:
                downstream, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                upstream = socket.create_connection(self._upstream, timeout=5)
            except OSError:
                downstream.close()
                continue
            downstream.settimeout(0.1)
            upstream.settimeout(0.1)
            with self._connections_lock:
                self._connections.extend((downstream, upstream))
            Thread(target=self._pipe, args=(downstream, upstream), daemon=True).start()
            Thread(target=self._pipe, args=(upstream, downstream), daemon=True).start()

    def _pipe(self, source: socket.socket, destination: socket.socket) -> None:
        while True:
            self._forwarding.wait()
            if self._stopped.is_set():
                return
            try:
                data = source.recv(64 * 1024)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                try:
                    destination.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                return
            try:
                destination.sendall(data)
            except OSError:
                return


def test_publish_uuid_deduplication_cas_and_large_message_pagination(
    admin: AdminClient, client: OpenEventClient
) -> None:
    principal = 1002
    token = _token(admin, principal)
    channel = client.create_channel(principal, token, "sdk-e2e-uuid").channel.channel_id
    first_uuid, second_uuid = client.get_uuid(), client.get_uuid()
    first_payload = b"a" * (10 * 1024 * 1024)
    second_payload = b"b" * (10 * 1024 * 1024)
    first = client.publish_auto_seq(principal, token, channel, first_payload, first_uuid).seq

    with pytest.raises(grpc.RpcError) as error:
        client.publish(principal, token, channel, first, b"later duplicate content is not compared", first_uuid)
    _assert_rpc_code(error, grpc.StatusCode.ALREADY_EXISTS)
    assert client.get_seq_by_uuid(first_uuid) == first
    with pytest.raises(grpc.RpcError) as error:
        client.publish(principal, token, channel, first, b"stale seq", second_uuid)
    _assert_rpc_code(error, grpc.StatusCode.ABORTED)
    second = first + 1
    client.publish(principal, token, channel, second, second_payload, second_uuid)

    cursor = first
    for seq, uuid, payload in ((first, first_uuid, first_payload), (second, second_uuid, second_payload)):
        page = client.fetch(principal, token, cursor, 100, channels=[channel])
        message, = page.messages
        assert (message.seq, message.uuid, message.payload) == (seq, uuid, payload)
        assert (message.channel_id, message.principal) == (channel, principal)
        assert page.last_seq == second
        assert page.next_seq > cursor
        cursor = page.next_seq
        if seq == first:
            assert cursor <= page.last_seq
    assert cursor > second


def test_subscription_replays_history_receives_new_messages_and_keeps_its_client_alive(admin, client, server_addresses):
    principal = 1401
    token = _token(admin, principal)
    system = client.get_channel(principal, token, 0)
    assert system.HasField("channel") and system.channel.channel_id == 0
    assert [c.channel_id for c in client.list_channels(principal, token).channels] == [0]
    system_message = admin.list_messages(0, 1).messages[0]
    assert system_message.seq == system_message.channel_id == system_message.principal == system_message.uuid == 0
    assert not system_message.recipients and not system_message.object_keys
    assert json.loads(system_message.payload) == {
        "kind": "system.initialization", "data": {"schema_version": 1},
    }

    client.create_channel(principal, token, "outside-subscription")
    channel_id = client.create_channel(
        principal=principal,
        token=token,
        name="sdk-e2e-subscribe",
        visibility=openevent_pb2.VISIBILITY_PUBLIC,
    ).channel.channel_id

    history_seq = client.publish_auto_seq(
        principal=principal,
        token=token,
        channel_id=channel_id,
        payload=b"subscribe history",
        uuid=client.get_uuid(),
    ).seq
    public, _ = server_addresses
    future_stream = OpenEventClient(public).subscribe(principal, token, channels=[channel_id, 0, channel_id])
    try:
        assert _next_response(future_stream, "seq 0").message == system_message
        initialized = _next_response(future_stream, "channel initialization").message
        _system_event_data(initialized, channel_id, "system.channel_initialized")
        assert _next_response(future_stream, f"history seq {history_seq}").message.seq == history_seq
        future_seq = client.publish_auto_seq(
            principal=principal,
            token=token,
            channel_id=channel_id,
            payload=b"subscribe future",
            uuid=client.get_uuid(),
        ).seq
        future = _next_response(future_stream, f"seq {future_seq}")
        assert future.message.seq == future_seq
        assert future.message.payload == b"subscribe future"
    finally:
        future_stream.cancel()

    idle_stream = client.subscribe(principal, token, from_seq=future_seq + 1)
    idle_stream.wait_started(5_000)
    ended = Event()
    idle_stream.add_done_callback(lambda _: ended.set())
    client.close()
    assert ended.wait(5)
    assert idle_stream.code() == grpc.StatusCode.CANCELLED


def test_filtered_fetch_and_subscription_and_unfiltered_admin_access(admin, client):
    sender, reader = 1401, 1402
    sender_token, reader_token = _token(admin, sender), _token(admin, reader)
    selected = client.create_channel(
        sender, sender_token, "selected", visibility=openevent_pb2.VISIBILITY_PRIVATE, members=[reader],
    ).channel.channel_id
    other = client.create_channel(sender, sender_token, "other", members=[reader]).channel.channel_id
    stream = client.subscribe(reader, reader_token, from_seq=0, channels=[selected], only_my_recipient=True)
    try:
        stream.wait_started(5_000)
        first = client.publish_auto_seq(
            sender, sender_token, selected, b"first", client.get_uuid(), recipients=[reader],
        ).seq
        other_seq = client.publish_auto_seq(
            sender, sender_token, other, b"other channel", client.get_uuid(), recipients=[reader],
        ).seq
        undirected = client.publish_auto_seq(sender, sender_token, selected, b"undirected", client.get_uuid()).seq
        last = client.publish_auto_seq(
            sender, sender_token, selected, b"last", client.get_uuid(), recipients=[reader],
        ).seq

        for seq, payload in ((first, b"first"), (last, b"last")):
            message = _next_response(stream, f"seq {seq}").message
            assert (message.seq, message.channel_id, message.payload) == (seq, selected, payload)
    finally:
        stream.cancel()

    channel_page = client.fetch(reader, reader_token, first, 100, channels=[selected])
    all_channels = client.fetch(reader, reader_token, first, 100, channels=[])
    assert [m.seq for m in channel_page.messages] == [first, undirected, last]
    assert [m.seq for m in all_channels.messages] == [first, other_seq, undirected, last]

    cursor, received = 0, []
    while True:
        page = client.fetch(reader, reader_token, cursor, 1, channels=[selected], only_my_recipient=True)
        assert page.next_seq > cursor
        received.extend((message.seq, message.payload) for message in page.messages)
        cursor = page.next_seq
        if cursor > page.last_seq:
            break
    assert received == [(first, b"first"), (last, b"last")]
    assert not client.fetch(sender, sender_token, first, 10, only_my_recipient=True).messages

    collected = []
    cursor = first
    while cursor <= last:
        page = admin.list_messages(from_seq=cursor, limit=1)
        assert page.last_seq >= last
        assert len(page.messages) <= 1
        collected.extend((message.seq, message.payload) for message in page.messages)
        assert page.next_seq > cursor
        cursor = page.next_seq
    assert collected == [
        (first, b"first"), (other_seq, b"other channel"), (undirected, b"undirected"), (last, b"last"),
    ]

    business_admin_stub = admin_pb2_grpc.AdminServiceStub(client.channel)
    with pytest.raises(grpc.RpcError) as exc_info:
        business_admin_stub.ListMessages(admin_pb2.ListMessagesRequest(from_seq=0, limit=1), timeout=5)
    _assert_rpc_code(exc_info, grpc.StatusCode.UNIMPLEMENTED)

    admin_event_stub = openevent_pb2_grpc.EventServiceStub(admin.channel)
    with pytest.raises(grpc.RpcError) as exc_info:
        admin_event_stub.GetStatus(openevent_pb2.GetStatusRequest(principal=reader, token=reader_token), timeout=5)
    _assert_rpc_code(exc_info, grpc.StatusCode.UNIMPLEMENTED)


def _assert_object_read_ranges_and_errors(client, key, data):
    middle = client.read_object(key.object_id, key.object_token, offset=2, nbytes=5)
    tail = client.read_object(key.object_id, key.object_token, offset=len(data) - 2, nbytes=(1 << 64) - 1)
    eof = client.read_object(key.object_id, key.object_token, offset=len(data), nbytes=1)
    assert middle.data == data[2:7]
    assert tail.data == data[-2:]
    assert eof.data == b""

    for token, offset, nbytes, code in [
        (key.object_token, len(data) + 1, 1, grpc.StatusCode.OUT_OF_RANGE),
        (key.object_token, 0, 0, grpc.StatusCode.INVALID_ARGUMENT),
        ("wrong-token", 0, 1, grpc.StatusCode.NOT_FOUND),
        ("", 0, 1, grpc.StatusCode.INVALID_ARGUMENT),
    ]:
        with pytest.raises(grpc.RpcError) as error:
            client.read_object(key.object_id, token, offset, nbytes)
        _assert_rpc_code(error, code)


def test_object_metadata_reads_and_message_references(admin: AdminClient, client: OpenEventClient) -> None:
    principal = 1801
    token = _token(admin, principal)
    with pytest.raises(grpc.RpcError) as error:
        client.write_object(
            principal, token, "oversized.bin", "application/octet-stream", b"x" * (4 * 1024 * 1024 + 1),
        )
    _assert_rpc_code(error, grpc.StatusCode.INVALID_ARGUMENT)

    def metadata_fields(metadata):
        return metadata.name, metadata.type, metadata.description, metadata.nbytes

    shared_payload = b"\x00\xffpayload"
    entries = [
        ("名" * 85, "型" * 85, "🙂" * 1023 + "a\x00bc", shared_payload),
        ("second.txt", "text/plain", "", "你好".encode("utf-8")),
    ]
    keys = []
    single_results = []
    for name, content_type, description, data in entries:
        written = client.write_object(
            principal=principal, token=token, name=name, type=content_type,
            description=description, data=data,
        )
        keys.append(openevent_pb2.ObjectKey(
            object_id=written.object_id, object_token=written.object_token,
        ))
        single = client.get_object_metadata(written.object_id, written.object_token)
        fields = metadata_fields(single)
        assert fields == (name, content_type, description, len(data))
        single_results.append(fields)

    order = [1, 0] + [1] * 1022
    response = client.batch_get_object_metadata(keys[index] for index in order)
    assert len(response.metadata) == 1024
    for item, index in zip(response.metadata, order):
        assert metadata_fields(item) == single_results[index]

    key = keys[0]
    _assert_object_read_ranges_and_errors(client, key, shared_payload)
    reader = 1802
    reader_token = _token(admin, reader)
    channel_id = client.create_channel(principal, token, "sdk-e2e-object-references").channel.channel_id
    published = client.publish_auto_seq(
        principal, token, channel_id, b"object references", client.get_uuid(), object_keys=[key],
    )
    message, = client.fetch(reader, reader_token, published.seq, 1, channels=[channel_id]).messages
    transferred_key, = message.object_keys
    transferred = client.read_object(transferred_key.object_id, transferred_key.object_token, offset=0, nbytes=1024)
    assert transferred.data == shared_payload


def test_public_and_private_channel_access_after_member_removal(admin, client):
    owner, member = 2101, 2102
    owner_token, member_token = _token(admin, owner), _token(admin, member)
    channel = client.create_channel(
        owner, owner_token, "private-membership", visibility=openevent_pb2.VISIBILITY_PRIVATE,
    ).channel.channel_id
    public_channel = client.create_channel(
        owner, owner_token, "public", visibility=openevent_pb2.VISIBILITY_PUBLIC, members=[member],
    ).channel
    assert member in public_channel.members
    public = public_channel.channel_id
    public_history = client.publish_auto_seq(member, member_token, public, b"before removal", client.get_uuid()).seq
    client.remove_member(owner, owner_token, public, member)
    assert member not in client.get_channel(owner, owner_token, public).channel.members

    used = client.get_uuid()
    with pytest.raises(grpc.RpcError) as error:
        client.publish_auto_seq(member, member_token, channel, b"before joining", used)
    assert error.value.code() == grpc.StatusCode.PERMISSION_DENIED
    client.add_member(owner, owner_token, channel, member)
    seq = client.publish_auto_seq(member, member_token, channel, b"as member", used).seq

    with ExitStack() as cleanup:
        explicit = client.subscribe(member, member_token, from_seq=seq, channels=[channel])
        cleanup.callback(explicit.cancel)
        all_readable = client.subscribe(member, member_token, from_seq=seq, channels=[])
        cleanup.callback(all_readable.cancel)
        for stream in (explicit, all_readable):
            message = _next_response(stream, f"seq {seq}").message
            assert (message.seq, message.payload) == (seq, b"as member")

        client.remove_member(owner, owner_token, channel, member)
        with pytest.raises(grpc.RpcError) as error:
            _next_response(explicit, "PERMISSION_DENIED")
        assert error.value.code() == grpc.StatusCode.PERMISSION_DENIED
        client.publish_auto_seq(owner, owner_token, channel, b"after-private", client.get_uuid())
        expected = client.publish_auto_seq(member, member_token, public, b"after-public", client.get_uuid()).seq
        assert _next_response(all_readable, f"seq {expected}").message.seq == expected

    for call in (
        lambda: client.publish_auto_seq(member, member_token, channel, b"after removal", client.get_uuid()),
        lambda: client.get_channel(member, member_token, channel),
        lambda: client.fetch(member, member_token, seq, 1, channels=[channel]),
    ):
        with pytest.raises(grpc.RpcError) as error:
            call()
        assert error.value.code() == grpc.StatusCode.PERMISSION_DENIED

    with pytest.raises(grpc.RpcError) as error:
        client.publish_auto_seq(member, member_token, channel, b"changed", used)
    assert error.value.code() == grpc.StatusCode.ALREADY_EXISTS
    history = client.fetch(member, member_token, public_history, 1, channels=[public])
    assert history.messages[0].payload == b"before removal"
    assert client.fetch(owner, owner_token, seq, 1, channels=[channel]).messages[0].payload == b"as member"


def test_removed_protected_member_keeps_read_and_subscription_access(admin, client):
    owner, member = 2201, 2202
    owner_token, member_token = _token(admin, owner), _token(admin, member)
    channel = client.create_channel(
        owner, owner_token, "protected-removal", visibility=openevent_pb2.VISIBILITY_PROTECTED, members=[member],
    ).channel.channel_id
    start = client.publish_auto_seq(member, member_token, channel, b"before", client.get_uuid()).seq
    stream = client.subscribe(member, member_token, from_seq=start, channels=[channel])
    try:
        assert _next_response(stream, f"seq {start}").message.seq == start
        client.remove_member(owner, owner_token, channel, member)
        retry_uuid = client.get_uuid()
        with pytest.raises(grpc.RpcError) as error:
            client.publish_auto_seq(member, member_token, channel, b"after", retry_uuid)
        assert error.value.code() == grpc.StatusCode.PERMISSION_DENIED
        assert member not in client.get_channel(member, member_token, channel).channel.members
        before, fetched_removal = client.fetch(member, member_token, start, 100, channels=[channel]).messages
        assert (before.seq, before.payload) == (start, b"before")
        removed = _next_response(stream, "member removal").message
        assert removed == fetched_removal
        assert _system_event_data(removed, channel, "system.member_removed")["member"] == member
        seq = client.publish_auto_seq(owner, owner_token, channel, b"after", retry_uuid, recipients=[owner]).seq
        assert seq == removed.seq + 1
        assert _next_response(stream, f"seq {seq}").message.seq == seq
    finally:
        stream.cancel()


def test_silent_disconnect_ends_subscription(admin, server_addresses):
    token = _token(admin, 2501)
    public, _ = server_addresses
    with ExitStack() as cleanup:
        relay = _TcpRelay(public)
        cleanup.callback(relay.close)
        # Keep an ordinary RPC deadline outside the 50-second disconnect window.
        proxied = cleanup.enter_context(OpenEventClient(relay.target, timeout_ms=120_000))
        stream = proxied.subscribe(2501, token, from_seq=0)
        cleanup.callback(stream.cancel)
        first = _next_response(stream, "initialization message")
        assert first.HasField("message") and first.message.seq == 0
        relay.pause()
        with pytest.raises(grpc.RpcError):
            _next_response(stream, "connection failure", timeout=50)


def test_subscription_acceptance_and_token_revocation(admin, client):
    principal = 2701
    token = _token(admin, principal)
    with pytest.raises(grpc.RpcError) as error:
        client.get_status(principal + 1, token)
    assert error.value.code() == grpc.StatusCode.UNAUTHENTICATED

    too_far = client.get_status(principal, token).max_seq + 2
    for fields, expected in [
        ({"principal": principal, "token": "wrong"}, grpc.StatusCode.UNAUTHENTICATED),
        ({"principal": principal, "token": token, "from_seq": too_far}, grpc.StatusCode.OUT_OF_RANGE),
        ({"principal": principal, "token": token, "channels": [999]}, grpc.StatusCode.NOT_FOUND),
    ]:
        stream = client.subscribe(**fields)
        try:
            with pytest.raises(grpc.RpcError) as error:
                stream.wait_started(5_000)
            assert error.value.code() == expected
            if expected == grpc.StatusCode.UNAUTHENTICATED:
                assert ("openevent-subscription", "accepted") not in stream.initial_metadata()
        finally:
            stream.cancel()
    stream = client.subscribe(principal, token, only_my_recipient=True)
    try:
        # Receive acceptance directly so wait_started is first called after stream termination.
        with ThreadPoolExecutor(max_workers=1) as executor:
            accepted_metadata = _wait_for_subscription(
                stream, executor.submit(stream.initial_metadata), "acceptance metadata"
            )
        assert ("openevent-subscription", "accepted") in accepted_metadata
        admin.delete_token(principal, token)
        with pytest.raises(grpc.RpcError) as error:
            _next_response(stream, "UNAUTHENTICATED")
        _assert_rpc_code(error, grpc.StatusCode.UNAUTHENTICATED)
        assert stream.wait_started(5_000) is None
        assert stream.wait_started(5_000) is None
        assert stream.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        stream.cancel()
    with pytest.raises(grpc.RpcError) as error:
        client.get_status(principal, token)
    assert error.value.code() == grpc.StatusCode.UNAUTHENTICATED
    admin.delete_token(principal, token)


def test_all_readable_subscription_discovers_new_channels_and_membership(admin, client):
    owner, agent = 2811, 2812
    token, agent_token = _token(admin, owner), _token(admin, agent)
    existing = client.create_channel(
        owner, token, "join-existing", visibility=openevent_pb2.VISIBILITY_PRIVATE,
    ).channel.channel_id
    start = client.get_status(agent, agent_token).max_seq + 1
    stream = client.subscribe(agent, agent_token, from_seq=start, channels=[])
    try:
        stream.wait_started(5_000)
        channel = client.create_channel(
            owner, token, "new-chat", protocol="chat.v1",
            visibility=openevent_pb2.VISIBILITY_PRIVATE, members=[agent],
        ).channel
        published = client.publish_auto_seq(
            owner, token, channel.channel_id, b"first business message", client.get_uuid(),
        ).seq
        initialized = _next_response(stream, "channel initialization").message
        _system_event_data(initialized, channel.channel_id, "system.channel_initialized")
        business = _next_response(stream, f"seq {published}").message
        assert initialized.seq == start and business.seq == published
        assert business.principal == owner and business.payload == b"first business message"

        client.publish_auto_seq(owner, token, existing, b"hidden", client.get_uuid())
        marker = client.publish_auto_seq(owner, token, channel.channel_id, b"visible marker", client.get_uuid()).seq
        assert _next_response(stream, f"seq {marker}").message.seq == marker
        client.add_member(owner, token, existing, agent)
        new_seq = client.publish_auto_seq(owner, token, existing, b"new", client.get_uuid()).seq
        added = _next_response(stream, "member addition").message
        assert _system_event_data(added, existing, "system.member_added")["member"] == agent
        visible = _next_response(stream, f"seq {new_seq}").message
        assert (visible.seq, visible.payload) == (new_seq, b"new")
    finally:
        stream.cancel()
