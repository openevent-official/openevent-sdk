from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
import json
import subprocess
import socket
from threading import Event, Lock, Thread
import time
from typing import Iterable, Iterator

import grpc
import pytest

from openevent.sdk import AdminClient, OpenEventClient
from openevent.sdk.proto import admin_pb2, admin_pb2_grpc, openevent_pb2, openevent_pb2_grpc


pytestmark = pytest.mark.skipif(
    os.environ.get("OPENEVENT_E2E") != "1",
    reason="set OPENEVENT_E2E=1 and run test-e2e.sh or make e2e",
)


def _event_target() -> str:
    return os.environ.get("OPENEVENT_E2E_TARGET", "127.0.0.1:19527")


def _admin_target() -> str:
    return os.environ.get("OPENEVENT_E2E_ADMIN_TARGET", "127.0.0.1:19528")


def _free_address():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"127.0.0.1:{sock.getsockname()[1]}"


@pytest.fixture
def server(tmp_path, monkeypatch):
    public = _free_address()
    admin_address = _free_address()
    while admin_address == public:
        admin_address = _free_address()
    monkeypatch.setenv("OPENEVENT_E2E_TARGET", public)
    monkeypatch.setenv("OPENEVENT_E2E_ADMIN_TARGET", admin_address)
    config = tmp_path / "server.yaml"
    config.write_text(
        f'grpc:\n  listen_addr: "{public}"\n'
        f'admin:\n  listen_addr: "{admin_address}"\n'
        f'storage:\n  path: "{tmp_path / "data"}"\n'
        'limits:\n  max_payload_bytes: 62914560\n'
    )
    with (tmp_path / "server.log").open("w") as log:
        process = subprocess.Popen([os.environ["OPENEVENT_SERVER_BIN"], str(config)], stdout=log, stderr=log)
        try:
            for address in (public, admin_address):
                with grpc.insecure_channel(address) as channel:
                    grpc.channel_ready_future(channel).result(timeout=10)
            assert process.poll() is None
            yield process
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
def admin(server) -> Iterator[AdminClient]:
    with AdminClient(_admin_target()) as value:
        yield value


@pytest.fixture
def client(server) -> Iterator[OpenEventClient]:
    with OpenEventClient(_event_target()) as value:
        yield value


def _token(admin: AdminClient, principal: int) -> str:
    return admin.add_token(principal=principal).token


def _unique(prefix: str) -> str:
    return f"{prefix}-{time.time_ns()}"


def _uuid(client: OpenEventClient) -> int:
    return client.get_uuid()


def _assert_rpc_code(exc_info: pytest.ExceptionInfo[grpc.RpcError], code: grpc.StatusCode) -> None:
    assert exc_info.value.code() == code


def _messages_by_payload(messages: Iterable, payload: bytes):
    return [message for message in messages if message.payload == payload]


def _take_messages(stream, count):
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(lambda: [next(stream).message for _ in range(count)])
        try:
            return pending.result(timeout=5)
        finally:
            stream.cancel()


def _assert_system_message(message, channel_id, kind):
    assert message.channel_id == channel_id
    assert message.seq > 0 and message.uuid > 0 and message.ts_ms > 0
    assert message.principal == 0
    assert not message.recipients and not message.object_keys
    envelope = json.loads(message.payload)
    assert set(envelope) == {"kind", "data"}
    assert envelope["kind"] == kind
    assert envelope["data"]["schema_version"] == 1
    return envelope["data"]


def _assert_channel_initialized(message, channel):
    data = _assert_system_message(message, channel.channel_id, "system.channel_initialized")
    assert sorted(data.pop("members")) == sorted(channel.members)
    assert data == {
        "schema_version": 1,
        "name": channel.name,
        "protocol": channel.protocol,
        "description": channel.description,
        "visibility": {0: "public", 1: "protected", 2: "private"}[channel.visibility],
        "creator": channel.creator,
    }


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
        while not self._stopped.is_set():
            if not self._forwarding.wait(0.1):
                continue
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


class _CommitThenAbortEventService(openevent_pb2_grpc.EventServiceServicer):
    def __init__(self, upstream_target: str):
        self._channel = grpc.insecure_channel(upstream_target)
        self._stub = openevent_pb2_grpc.EventServiceStub(self._channel)

    def Publish(self, request, context):
        self._stub.Publish(request)
        context.abort(grpc.StatusCode.UNAVAILABLE, "test proxy dropped committed Publish response")

    def PublishAutoSeq(self, request, context):
        self._stub.PublishAutoSeq(request)
        context.abort(grpc.StatusCode.UNAVAILABLE, "test proxy dropped committed PublishAutoSeq response")

    def close(self) -> None:
        self._channel.close()


class _CommitThenAbortObjectService(openevent_pb2_grpc.ObjectStorageServiceServicer):
    def __init__(self, upstream_target: str):
        self._channel = grpc.insecure_channel(upstream_target)
        self._stub = openevent_pb2_grpc.ObjectStorageServiceStub(self._channel)
        self.write_calls = 0
        self.committed_response = None

    def WriteObject(self, request, context):
        self.write_calls += 1
        self.committed_response = self._stub.WriteObject(request)
        context.abort(grpc.StatusCode.UNAVAILABLE, "test proxy dropped committed WriteObject response")

    def close(self) -> None:
        self._channel.close()


@contextmanager
def _commit_then_abort_client() -> Iterator[OpenEventClient]:
    service = _CommitThenAbortEventService(_event_target())
    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    openevent_pb2_grpc.add_EventServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    assert port > 0
    server.start()
    proxy_client = OpenEventClient(f"127.0.0.1:{port}")
    try:
        yield proxy_client
    finally:
        proxy_client.channel.close()
        server.stop(0).wait()
        service.close()


@contextmanager
def _commit_then_abort_object_client():
    service = _CommitThenAbortObjectService(_event_target())
    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    openevent_pb2_grpc.add_ObjectStorageServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    assert port > 0
    server.start()
    proxy_client = OpenEventClient(f"127.0.0.1:{port}")
    try:
        yield proxy_client, service
    finally:
        proxy_client.channel.close()
        server.stop(0).wait()
        service.close()


def test_publish_auto_seq_and_fetch_round_trip(admin: AdminClient, client: OpenEventClient) -> None:
    principal = 1001
    token = _token(admin, principal)
    channel = client.create_channel(
        principal=principal,
        token=token,
        name=_unique("sdk-e2e-public"),
        visibility=openevent_pb2.VISIBILITY_PUBLIC,
        protocol="text/plain",
        description="sdk e2e public channel",
    ).channel

    published = client.publish_auto_seq(
        principal=principal,
        token=token,
        channel_id=channel.channel_id,
        payload=b"hello e2e",
        uuid=_uuid(client),
    )
    status = client.get_status(principal=principal, token=token)
    fetched = client.fetch(
        principal=principal,
        token=token,
        from_seq=1,
        limit=1000,
        channels=[channel.channel_id],
    )

    matches = [message for message in fetched.messages if message.seq == published.seq]
    assert channel.channel_id > 0
    assert published.seq > 0
    assert status.max_seq >= published.seq
    assert fetched.last_seq >= published.seq
    assert fetched.next_seq > 0
    assert len(matches) == 1
    assert matches[0].channel_id == channel.channel_id
    assert matches[0].principal == principal
    assert matches[0].payload == b"hello e2e"
    assert matches[0].ts_ms > 0


def test_get_uuid_and_reject_duplicate_uuid(admin: AdminClient, client: OpenEventClient) -> None:
    principal = 1002
    token = _token(admin, principal)
    channel = client.create_channel(
        principal=principal,
        token=token,
        name=_unique("sdk-e2e-uuid"),
        visibility=openevent_pb2.VISIBILITY_PUBLIC,
    ).channel

    first_uuid = client.get_uuid()
    second_uuid = client.get_uuid()
    assert second_uuid == first_uuid + 1

    assert client.get_seq_by_uuid(0) == 0
    with pytest.raises(grpc.RpcError) as exc_info:
        client.get_seq_by_uuid(second_uuid)
    _assert_rpc_code(exc_info, grpc.StatusCode.NOT_FOUND)

    first = client.publish_auto_seq(
        principal=principal,
        token=token,
        channel_id=channel.channel_id,
        payload=b"first UUID write",
        uuid=first_uuid,
    )
    with pytest.raises(grpc.RpcError) as exc_info:
        client.publish_auto_seq(
            principal=principal,
            token=token,
            channel_id=channel.channel_id,
            payload=b"later duplicate content is not compared",
            uuid=first_uuid,
        )
    _assert_rpc_code(exc_info, grpc.StatusCode.ALREADY_EXISTS)
    assert client.get_seq_by_uuid(first_uuid) == first.seq

    fetched = client.fetch(principal=principal, token=token, from_seq=first.seq, limit=1)
    assert fetched.messages[0].uuid == first_uuid


def test_publish_cas_success_and_aborted_reuse(admin: AdminClient, client: OpenEventClient) -> None:
    principal = 1101
    token = _token(admin, principal)
    channel_id = client.create_channel(
        principal=principal,
        token=token,
        name=_unique("sdk-e2e-cas"),
        visibility=openevent_pb2.VISIBILITY_PUBLIC,
    ).channel.channel_id
    next_seq = client.get_status(principal=principal, token=token).max_seq + 1

    client.publish(
        principal=principal,
        token=token,
        channel_id=channel_id,
        seq=next_seq,
        payload=b"cas-ok",
        uuid=_uuid(client),
    )
    with pytest.raises(grpc.RpcError) as exc_info:
        client.publish(
            principal=principal,
            token=token,
            channel_id=channel_id,
            seq=next_seq,
            payload=b"cas-repeat",
            uuid=_uuid(client),
        )
    _assert_rpc_code(exc_info, grpc.StatusCode.ABORTED)


def test_private_channel_acl_and_member_management(admin: AdminClient, client: OpenEventClient) -> None:
    owner = 1201
    member = 1202
    outsider = 1203
    owner_token = _token(admin, owner)
    member_token = _token(admin, member)
    outsider_token = _token(admin, outsider)
    channel_id = client.create_channel(
        principal=owner,
        token=owner_token,
        name=_unique("sdk-e2e-private"),
        visibility=openevent_pb2.VISIBILITY_PRIVATE,
    ).channel.channel_id

    client.add_member(principal=owner, token=owner_token, channel_id=channel_id, target_principal=member)
    member_seq = client.publish_auto_seq(
        principal=member,
        token=member_token,
        channel_id=channel_id,
        payload=b"member can write",
        uuid=_uuid(client),
    ).seq
    member_fetch = client.fetch(principal=member, token=member_token, from_seq=member_seq, limit=10)
    assert _messages_by_payload(member_fetch.messages, b"member can write")

    with pytest.raises(grpc.RpcError) as exc_info:
        client.get_channel(principal=outsider, token=outsider_token, channel_id=channel_id)
    _assert_rpc_code(exc_info, grpc.StatusCode.PERMISSION_DENIED)

    with pytest.raises(grpc.RpcError) as exc_info:
        client.publish_auto_seq(
            principal=outsider,
            token=outsider_token,
            channel_id=channel_id,
            payload=b"outsider blocked",
            uuid=_uuid(client),
        )
    _assert_rpc_code(exc_info, grpc.StatusCode.PERMISSION_DENIED)

    with pytest.raises(grpc.RpcError) as exc_info:
        client.fetch(
            principal=outsider,
            token=outsider_token,
            from_seq=member_seq,
            limit=10,
            channels=[channel_id],
        )
    _assert_rpc_code(exc_info, grpc.StatusCode.PERMISSION_DENIED)

    outsider_stream = client.subscribe(
        principal=outsider,
        token=outsider_token,
        from_seq=member_seq,
        channels=[channel_id],
    )
    with pytest.raises(grpc.RpcError) as exc_info:
        next(outsider_stream)
    _assert_rpc_code(exc_info, grpc.StatusCode.PERMISSION_DENIED)

    client.remove_member(principal=owner, token=owner_token, channel_id=channel_id, target_principal=member)
    with pytest.raises(grpc.RpcError) as exc_info:
        client.publish_auto_seq(
            principal=member,
            token=member_token,
            channel_id=channel_id,
            payload=b"former member blocked",
            uuid=_uuid(client),
        )
    _assert_rpc_code(exc_info, grpc.StatusCode.PERMISSION_DENIED)


def test_recipients_filtering(admin: AdminClient, client: OpenEventClient) -> None:
    owner = 1301
    recipient = 1302
    other = 1303
    owner_token = _token(admin, owner)
    recipient_token = _token(admin, recipient)
    other_token = _token(admin, other)
    channel_id = client.create_channel(
        principal=owner,
        token=owner_token,
        name=_unique("sdk-e2e-recipients"),
        visibility=openevent_pb2.VISIBILITY_PROTECTED,
        members=[recipient, other],
    ).channel.channel_id

    direct_seq = client.publish_auto_seq(
        principal=owner,
        token=owner_token,
        channel_id=channel_id,
        recipients=[recipient],
        payload=b"direct recipient",
        uuid=_uuid(client),
    ).seq
    client.publish_auto_seq(
        principal=owner,
        token=owner_token,
        channel_id=channel_id,
        payload=b"broadcast without recipients",
        uuid=_uuid(client),
    )

    recipient_messages = client.fetch(
        principal=recipient,
        token=recipient_token,
        from_seq=direct_seq,
        limit=10,
        only_my_recipient=True,
    ).messages
    other_messages = client.fetch(
        principal=other,
        token=other_token,
        from_seq=direct_seq,
        limit=10,
        only_my_recipient=True,
    ).messages

    assert [message.payload for message in recipient_messages] == [b"direct recipient"]
    assert b"direct recipient" not in [message.payload for message in other_messages]
    assert b"broadcast without recipients" not in [message.payload for message in recipient_messages]


def test_fetch_channel_filter_and_last_seq(admin: AdminClient, client: OpenEventClient) -> None:
    principal = 1351
    token = _token(admin, principal)
    first_channel_id = client.create_channel(
        principal=principal,
        token=token,
        name=_unique("sdk-e2e-fetch-first"),
        visibility=openevent_pb2.VISIBILITY_PUBLIC,
    ).channel.channel_id
    second_channel_id = client.create_channel(
        principal=principal,
        token=token,
        name=_unique("sdk-e2e-fetch-second"),
        visibility=openevent_pb2.VISIBILITY_PUBLIC,
    ).channel.channel_id

    first_seq = client.publish_auto_seq(
        principal=principal,
        token=token,
        channel_id=first_channel_id,
        payload=b"fetch first channel",
        uuid=_uuid(client),
    ).seq
    second_seq = client.publish_auto_seq(
        principal=principal,
        token=token,
        channel_id=second_channel_id,
        payload=b"fetch second channel",
        uuid=_uuid(client),
    ).seq
    max_published_seq = max(first_seq, second_seq)

    first_fetch = client.fetch(
        principal=principal,
        token=token,
        from_seq=first_seq,
        limit=1000,
        channels=[first_channel_id],
    )
    second_fetch = client.fetch(
        principal=principal,
        token=token,
        from_seq=first_seq,
        limit=1000,
        channels=[second_channel_id],
    )
    all_fetch = client.fetch(
        principal=principal,
        token=token,
        from_seq=first_seq,
        limit=1000,
        channels=[],
    )

    assert b"fetch first channel" in [message.payload for message in first_fetch.messages]
    assert b"fetch second channel" not in [message.payload for message in first_fetch.messages]
    assert b"fetch second channel" in [message.payload for message in second_fetch.messages]
    assert b"fetch first channel" not in [message.payload for message in second_fetch.messages]
    assert {b"fetch first channel", b"fetch second channel"}.issubset(
        {message.payload for message in all_fetch.messages}
    )
    for response in (first_fetch, second_fetch, all_fetch):
        assert response.last_seq >= max_published_seq
        assert response.next_seq > response.last_seq

    with pytest.raises(grpc.RpcError) as exc_info:
        client.fetch(
            principal=principal,
            token=token,
            from_seq=first_seq,
            limit=1000,
            channels=[2**64 - 1],
        )
    _assert_rpc_code(exc_info, grpc.StatusCode.NOT_FOUND)


def test_subscribe_from_history_and_future_boundary(admin: AdminClient, client: OpenEventClient) -> None:
    principal = 1401
    token = _token(admin, principal)
    channel_id = client.create_channel(
        principal=principal,
        token=token,
        name=_unique("sdk-e2e-subscribe"),
        visibility=openevent_pb2.VISIBILITY_PUBLIC,
    ).channel.channel_id

    history_seq = client.publish_auto_seq(
        principal=principal,
        token=token,
        channel_id=channel_id,
        payload=b"subscribe history",
        uuid=_uuid(client),
    ).seq
    history_stream = client.subscribe(principal=principal, token=token, from_seq=history_seq)
    first = next(history_stream)
    history_stream.cancel()
    assert first.message.seq == history_seq
    assert first.message.payload == b"subscribe history"

    future_stream = client.subscribe(principal=principal, token=token, from_seq=0)
    assert next(future_stream).message.seq == 0
    initialization = next(future_stream).message
    assert initialization.channel_id == channel_id and initialization.principal == 0
    assert initialization.seq < history_seq
    assert next(future_stream).message.seq == history_seq
    executor = ThreadPoolExecutor(max_workers=1)
    pending = executor.submit(next, future_stream)
    try:
        time.sleep(0.2)
        future_seq = client.publish_auto_seq(
            principal=principal,
            token=token,
            channel_id=channel_id,
            payload=b"subscribe future",
            uuid=_uuid(client),
        ).seq
        future = pending.result(timeout=5)
        assert future.message.seq == future_seq
        assert future.message.payload == b"subscribe future"
    finally:
        future_stream.cancel()
        executor.shutdown(wait=True, cancel_futures=True)

    max_seq = client.get_status(principal=principal, token=token).max_seq
    boundary = client.subscribe(principal=principal, token=token, from_seq=max_seq + 1)
    boundary.wait_started(5_000)
    assert ("openevent-subscription", "accepted") in boundary.initial_metadata()
    executor = ThreadPoolExecutor(max_workers=1)
    pending = executor.submit(next, boundary)
    try:
        boundary_seq = client.publish_auto_seq(
            principal=principal,
            token=token,
            channel_id=channel_id,
            payload=b"subscribe boundary",
            uuid=_uuid(client),
        ).seq
        boundary_item = pending.result(timeout=5)
        assert boundary_item.message.seq == boundary_seq
        assert boundary_item.message.payload == b"subscribe boundary"
    finally:
        boundary.cancel()
        executor.shutdown(wait=True, cancel_futures=True)

    too_far = client.get_status(principal=principal, token=token).max_seq + 2
    invalid_boundary = client.subscribe(principal=principal, token=token, from_seq=too_far)
    with pytest.raises(grpc.RpcError) as exc_info:
        invalid_boundary.wait_started(5_000)
    _assert_rpc_code(exc_info, grpc.StatusCode.OUT_OF_RANGE)
    assert ("openevent-subscription", "accepted") not in invalid_boundary.initial_metadata()


def test_subscribe_channel_filter(admin: AdminClient, client: OpenEventClient) -> None:
    principal = 1402
    token = _token(admin, principal)
    first_channel_id = client.create_channel(
        principal=principal,
        token=token,
        name=_unique("sdk-e2e-subscribe-first"),
        visibility=openevent_pb2.VISIBILITY_PUBLIC,
    ).channel.channel_id
    selected_channel_id = client.create_channel(
        principal=principal,
        token=token,
        name=_unique("sdk-e2e-subscribe-selected"),
        visibility=openevent_pb2.VISIBILITY_PUBLIC,
    ).channel.channel_id
    first_seq = client.publish_auto_seq(
        principal=principal,
        token=token,
        channel_id=first_channel_id,
        payload=b"subscribe filtered channel",
        uuid=_uuid(client),
    ).seq
    selected_seq = client.publish_auto_seq(
        principal=principal,
        token=token,
        channel_id=selected_channel_id,
        payload=b"subscribe selected channel",
        uuid=_uuid(client),
    ).seq
    client.publish_auto_seq(
        principal=principal,
        token=token,
        channel_id=first_channel_id,
        payload=b"subscribe filtered gap",
        uuid=_uuid(client),
    )
    later_seq = client.publish_auto_seq(
        principal=principal,
        token=token,
        channel_id=selected_channel_id,
        payload=b"subscribe after gap",
        uuid=_uuid(client),
    ).seq

    stream = client.subscribe(
        principal=principal,
        token=token,
        from_seq=first_seq,
        channels=[selected_channel_id],
    )
    try:
        response = next(stream)
        later = next(stream)
    finally:
        stream.cancel()
    assert response.message.seq == selected_seq
    assert response.message.channel_id == selected_channel_id
    assert response.message.payload == b"subscribe selected channel"
    assert later.message.seq == later_seq
    assert later_seq > selected_seq + 1
    assert later.message.channel_id == selected_channel_id
    assert later.message.payload == b"subscribe after gap"

    replay = client.subscribe(
        principal=principal,
        token=token,
        from_seq=selected_seq,
        channels=[selected_channel_id],
    )
    try:
        assert next(replay).message.seq == selected_seq
    finally:
        replay.cancel()

    missing_stream = client.subscribe(
        principal=principal,
        token=token,
        from_seq=first_seq,
        channels=[2**64 - 1],
    )
    with pytest.raises(grpc.RpcError) as exc_info:
        next(missing_stream)
    _assert_rpc_code(exc_info, grpc.StatusCode.NOT_FOUND)


def test_subscribe_wait_started_before_selected_channel_message(
    admin: AdminClient, client: OpenEventClient
) -> None:
    principal = 1403
    token = _token(admin, principal)
    skipped_channel_id = client.create_channel(
        principal=principal,
        token=token,
        name=_unique("sdk-e2e-subscribe-skipped"),
        visibility=openevent_pb2.VISIBILITY_PUBLIC,
    ).channel.channel_id
    selected_channel_id = client.create_channel(
        principal=principal,
        token=token,
        name=_unique("sdk-e2e-subscribe-wait-started"),
        visibility=openevent_pb2.VISIBILITY_PUBLIC,
    ).channel.channel_id
    start_seq = client.publish_auto_seq(
        principal=principal,
        token=token,
        channel_id=skipped_channel_id,
        payload=b"subscribe wait-started skipped channel",
        uuid=_uuid(client),
    ).seq

    stream = client.subscribe(
        principal=principal,
        token=token,
        from_seq=start_seq,
        channels=[selected_channel_id],
    )
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        stream.wait_started(5_000)
        assert ("openevent-subscription", "accepted") in stream.initial_metadata()
        pending = executor.submit(next, stream)
        time.sleep(0.1)
        assert not pending.done()
        selected_seq = client.publish_auto_seq(
            principal=principal,
            token=token,
            channel_id=selected_channel_id,
            payload=b"subscribe wait-started selected channel",
            uuid=_uuid(client),
        ).seq
        response = pending.result(timeout=5)
    finally:
        stream.cancel()
        executor.shutdown(wait=True, cancel_futures=True)

    assert response.message.seq == selected_seq
    assert response.message.channel_id == selected_channel_id


def test_subscribe_keepalive_closes_a_silently_dropped_transport(admin: AdminClient) -> None:
    principal = 1404
    token = _token(admin, principal)
    relay = _TcpRelay(_event_target())
    client = OpenEventClient(relay.target)
    stream = client.subscribe(principal=principal, token=token, from_seq=0)
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        stream.wait_started(5_000)
        assert next(stream).message.seq == 0
        pending = executor.submit(next, stream)
        time.sleep(0.1)
        relay.pause()
        with pytest.raises(grpc.RpcError) as exc_info:
            pending.result(timeout=50)
        assert exc_info.value.code() != grpc.StatusCode.OK
    finally:
        stream.cancel()
        client.close()
        relay.close()
        executor.shutdown(wait=True, cancel_futures=True)


def test_token_delete_changes_authentication(admin: AdminClient, client: OpenEventClient) -> None:
    principal = 1501
    token = _token(admin, principal)
    other_token = _token(admin, principal)
    assert token and other_token
    client.get_status(principal=principal, token=token)
    client.get_status(principal=principal, token=other_token)

    with pytest.raises(grpc.RpcError) as exc_info:
        client.get_status(principal=principal + 1, token=token)
    _assert_rpc_code(exc_info, grpc.StatusCode.UNAUTHENTICATED)

    # Deleting an absent pair under another principal must leave this pair valid.
    admin.delete_token(principal=principal + 1, token=token)
    client.get_status(principal=principal, token=token)
    admin.delete_token(principal=principal, token=token)
    admin.delete_token(principal=principal, token=token)

    with pytest.raises(grpc.RpcError) as exc_info:
        client.get_status(principal=principal, token=token)
    _assert_rpc_code(exc_info, grpc.StatusCode.UNAUTHENTICATED)
    if other_token == token:
        with pytest.raises(grpc.RpcError) as exc_info:
            client.get_status(principal=principal, token=other_token)
        _assert_rpc_code(exc_info, grpc.StatusCode.UNAUTHENTICATED)
    else:
        client.get_status(principal=principal, token=other_token)


def test_admin_lists_all_messages_and_ports_are_isolated(
    admin: AdminClient, client: OpenEventClient
) -> None:
    principal = 1601
    member = 1602
    outsider = 1603
    token = _token(admin, principal)
    outsider_token = _token(admin, outsider)
    public_channel_id = client.create_channel(
        principal=principal,
        token=token,
        name=_unique("sdk-e2e-admin-public"),
        visibility=openevent_pb2.VISIBILITY_PUBLIC,
    ).channel.channel_id
    private_channel_id = client.create_channel(
        principal=principal,
        token=token,
        name=_unique("sdk-e2e-admin-private"),
        visibility=openevent_pb2.VISIBILITY_PRIVATE,
        members=[member],
    ).channel.channel_id
    public_payload = _unique("admin-public").encode()
    private_payload = _unique("admin-private").encode()
    recipient_payload = _unique("admin-recipient").encode()
    public_seq = client.publish_auto_seq(
        principal=principal,
        token=token,
        channel_id=public_channel_id,
        payload=public_payload,
        uuid=_uuid(client),
    ).seq
    private_seq = client.publish_auto_seq(
        principal=principal,
        token=token,
        channel_id=private_channel_id,
        payload=private_payload,
        uuid=_uuid(client),
    ).seq
    recipient_seq = client.publish_auto_seq(
        principal=principal,
        token=token,
        channel_id=private_channel_id,
        recipients=[member],
        payload=recipient_payload,
        uuid=_uuid(client),
    ).seq

    first_page = admin.list_messages(from_seq=0, limit=1)
    boundary = first_page.last_seq
    assert boundary >= max(public_seq, private_seq, recipient_seq)
    appended_payload = _unique("admin-after-boundary").encode()
    appended_seq = client.publish_auto_seq(
        principal=principal,
        token=token,
        channel_id=public_channel_id,
        payload=appended_payload,
        uuid=_uuid(client),
    ).seq
    assert appended_seq > boundary

    collected = [message for message in first_page.messages if message.seq <= boundary]
    next_seq = first_page.next_seq
    seen_cursors = {next_seq}
    while next_seq <= boundary:
        page = admin.list_messages(from_seq=next_seq, limit=1)
        assert len(page.messages) <= 1
        collected.extend(message for message in page.messages if message.seq <= boundary)
        assert page.next_seq > next_seq
        next_seq = page.next_seq
        if next_seq <= boundary:
            assert next_seq not in seen_cursors
            seen_cursors.add(next_seq)

    collected_seqs = [message.seq for message in collected]
    assert collected_seqs == sorted(collected_seqs)
    assert len(collected_seqs) == len(set(collected_seqs))
    collected_payloads = {message.payload for message in collected}
    assert {public_payload, private_payload, recipient_payload}.issubset(collected_payloads)
    assert appended_payload not in collected_payloads

    with pytest.raises(grpc.RpcError) as exc_info:
        client.fetch(
            principal=outsider,
            token=outsider_token,
            from_seq=private_seq,
            limit=1000,
            channels=[private_channel_id],
        )
    _assert_rpc_code(exc_info, grpc.StatusCode.PERMISSION_DENIED)

    with pytest.raises(grpc.RpcError) as exc_info:
        admin.list_messages(from_seq=0, limit=0)
    _assert_rpc_code(exc_info, grpc.StatusCode.INVALID_ARGUMENT)
    with pytest.raises(grpc.RpcError) as exc_info:
        admin.list_messages(from_seq=0, limit=1001)
    _assert_rpc_code(exc_info, grpc.StatusCode.INVALID_ARGUMENT)

    business_admin_stub = admin_pb2_grpc.AdminServiceStub(grpc.insecure_channel(_event_target()))
    with pytest.raises(grpc.RpcError) as exc_info:
        business_admin_stub.ListMessages(admin_pb2.ListMessagesRequest(from_seq=0, limit=1))
    _assert_rpc_code(exc_info, grpc.StatusCode.UNIMPLEMENTED)

    admin_event_stub = openevent_pb2_grpc.EventServiceStub(grpc.insecure_channel(_admin_target()))
    with pytest.raises(grpc.RpcError) as exc_info:
        admin_event_stub.GetStatus(openevent_pb2.GetStatusRequest(principal=principal, token=token))
    _assert_rpc_code(exc_info, grpc.StatusCode.UNIMPLEMENTED)


def test_uncertain_publish_result_is_reconciled_by_uuid_lookup(
    admin: AdminClient, client: OpenEventClient
) -> None:
    principal = 1701
    token = _token(admin, principal)
    channel_id = client.create_channel(
        principal=principal,
        token=token,
        name=_unique("sdk-e2e-reconcile"),
        visibility=openevent_pb2.VISIBILITY_PUBLIC,
    ).channel.channel_id

    auto_before = client.get_status(principal=principal, token=token).max_seq
    auto_payload = b"uncertain auto publish"
    auto_uuid = _uuid(client)
    with _commit_then_abort_client() as proxy:
        with pytest.raises(grpc.RpcError) as exc_info:
            proxy.publish_auto_seq(
                principal=principal,
                token=token,
                channel_id=channel_id,
                payload=auto_payload,
                uuid=auto_uuid,
            )
        _assert_rpc_code(exc_info, grpc.StatusCode.UNAVAILABLE)
    with pytest.raises(grpc.RpcError) as exc_info:
        client.publish_auto_seq(
            principal=principal,
            token=token,
            channel_id=channel_id,
            payload=b"later PublishAutoSeq content is not compared",
            uuid=auto_uuid,
        )
    _assert_rpc_code(exc_info, grpc.StatusCode.ALREADY_EXISTS)
    assert client.get_seq_by_uuid(auto_uuid) == auto_before + 1

    cas_before = client.get_status(principal=principal, token=token).max_seq
    cas_seq = cas_before + 1
    cas_payload = b"uncertain cas publish"
    cas_uuid = _uuid(client)
    with _commit_then_abort_client() as proxy:
        with pytest.raises(grpc.RpcError) as exc_info:
            proxy.publish(
                principal=principal,
                token=token,
                channel_id=channel_id,
                seq=cas_seq,
                payload=cas_payload,
                uuid=cas_uuid,
            )
        _assert_rpc_code(exc_info, grpc.StatusCode.UNAVAILABLE)
    with pytest.raises(grpc.RpcError) as exc_info:
        client.publish(
            principal=principal,
            token=token,
            channel_id=channel_id,
            seq=cas_seq,
            payload=b"later Publish content is not compared",
            uuid=cas_uuid,
        )
    _assert_rpc_code(exc_info, grpc.StatusCode.ALREADY_EXISTS)
    assert client.get_seq_by_uuid(cas_uuid) == cas_seq


@pytest.mark.parametrize("method", ["publish", "publish_auto_seq"])
@pytest.mark.parametrize(
    "visibility",
    [openevent_pb2.VISIBILITY_PROTECTED, openevent_pb2.VISIBILITY_PRIVATE],
    ids=["protected", "private"],
)
def test_uncertain_publish_retry_after_member_removal(
    admin: AdminClient, client: OpenEventClient, method: str, visibility: int
) -> None:
    owner, member = 1711, 1712
    owner_token = _token(admin, owner)
    member_token = _token(admin, member)
    channel_id = client.create_channel(
        principal=owner,
        token=owner_token,
        name=_unique("sdk-e2e-reconcile-after-removal"),
        visibility=visibility,
        members=[member],
    ).channel.channel_id
    committed_seq = client.get_status(principal=owner, token=owner_token).max_seq + 1
    original_uuid = client.get_uuid()
    request = {
        "principal": member,
        "token": member_token,
        "channel_id": channel_id,
        "payload": b"committed before member removal",
        "uuid": original_uuid,
    }
    if method == "publish":
        request["seq"] = committed_seq

    with _commit_then_abort_client() as proxy:
        with pytest.raises(grpc.RpcError) as exc_info:
            getattr(proxy, method)(**request)
        _assert_rpc_code(exc_info, grpc.StatusCode.UNAVAILABLE)

    client.remove_member(
        principal=owner,
        token=owner_token,
        channel_id=channel_id,
        target_principal=member,
    )
    # The member's token remains valid; only the Channel permission was revoked.
    after_removal = client.get_status(principal=member, token=member_token).max_seq
    assert after_removal == committed_seq + 1

    # Reuse the exact uncertain request, including Publish's now-stale CAS seq.
    with pytest.raises(grpc.RpcError) as exc_info:
        getattr(client, method)(**request)
    _assert_rpc_code(exc_info, grpc.StatusCode.ALREADY_EXISTS)
    assert client.get_seq_by_uuid(original_uuid) == committed_seq

    new_request = {**request, "uuid": client.get_uuid()}
    if method == "publish":
        new_request["seq"] = after_removal + 1
    with pytest.raises(grpc.RpcError) as exc_info:
        getattr(client, method)(**new_request)
    _assert_rpc_code(exc_info, grpc.StatusCode.PERMISSION_DENIED)
    assert client.get_status(principal=owner, token=owner_token).max_seq == after_removal

    messages = client.fetch(
        principal=owner,
        token=owner_token,
        from_seq=committed_seq,
        limit=10,
        channels=[channel_id],
    ).messages
    matching = [message for message in messages if message.uuid == original_uuid]
    assert len(matching) == 1
    assert matching[0].seq == committed_seq
    assert matching[0].principal == member
    assert matching[0].payload == request["payload"]


def test_object_write_metadata_and_partial_read(admin: AdminClient, client: OpenEventClient) -> None:
    principal = 1801
    token = _token(admin, principal)
    data = b"object\x00payload"
    written = client.write_object(
        principal=principal,
        token=token,
        name="payload.bin",
        type="application/octet-stream",
        description="sdk e2e object",
        data=data,
    )
    assert written.object_id > 0
    assert len(written.object_token) == 43

    key = dict(object_id=written.object_id, object_token=written.object_token)
    metadata = client.get_object_metadata(**key)
    assert metadata.name == "payload.bin"
    assert metadata.type == "application/octet-stream"
    assert metadata.description == "sdk e2e object"
    assert metadata.nbytes == len(data)

    full = client.read_object(**key, offset=0, nbytes=(1 << 64) - 1)
    middle = client.read_object(**key, offset=2, nbytes=5)
    tail = client.read_object(**key, offset=len(data) - 2, nbytes=100)
    eof = client.read_object(**key, offset=len(data), nbytes=1)
    assert full.data == data
    assert middle.data == data[2:7]
    assert tail.data == data[-2:]
    assert eof.data == b""

    invalid_reads = [
        (
            (written.object_id, written.object_token, len(data) + 1, 1),
            grpc.StatusCode.OUT_OF_RANGE,
        ),
        (
            (written.object_id, written.object_token, 0, 0),
            grpc.StatusCode.INVALID_ARGUMENT,
        ),
        (
            (written.object_id, "wrong-token", 0, 1),
            grpc.StatusCode.NOT_FOUND,
        ),
        (
            (written.object_id + (1 << 32), written.object_token, 0, 1),
            grpc.StatusCode.NOT_FOUND,
        ),
        (
            (0, written.object_token, 0, 1),
            grpc.StatusCode.INVALID_ARGUMENT,
        ),
        (
            (written.object_id, "", 0, 1),
            grpc.StatusCode.INVALID_ARGUMENT,
        ),
    ]
    for request_args, expected_code in invalid_reads:
        with pytest.raises(grpc.RpcError) as exc_info:
            client.read_object(*request_args)
        _assert_rpc_code(exc_info, expected_code)

    invalid_writes = [
        dict(name="", type="type", data=b"x"),
        dict(name="name", type="", data=b"x"),
        dict(name="name", type="type", data=b""),
        dict(name="n" * 256, type="type", data=b"x"),
        dict(name="name", type="t" * 256, data=b"x"),
        dict(name="name", type="type", description="d" * 4097, data=b"x"),
        dict(name="name", type="type", data=b"x" * (4 * 1024 * 1024 + 1)),
    ]
    for request_args in invalid_writes:
        with pytest.raises(grpc.RpcError) as exc_info:
            client.write_object(principal=principal, token=token, **request_args)
        _assert_rpc_code(exc_info, grpc.StatusCode.INVALID_ARGUMENT)

    next_object = client.write_object(
        principal=principal,
        token=token,
        name="next",
        type="application/octet-stream",
        data=b"next",
    )
    assert next_object.object_id > written.object_id


def test_batch_object_metadata_preserves_order_duplicates_and_fields(
    admin: AdminClient, client: OpenEventClient
) -> None:
    principal = 1802
    token = _token(admin, principal)
    entries = [
        ("附件.bin", "application/octet-stream", "说明：原样保留\x00结束", b"\x00\xffpayload"),
        ("second.txt", "text/plain", "", "你好".encode("utf-8")),
    ]
    keys = []
    expected = []
    for name, content_type, description, data in entries:
        written = client.write_object(
            principal=principal, token=token, name=name, type=content_type,
            description=description, data=data,
        )
        keys.append(openevent_pb2.ObjectKey(
            object_id=written.object_id, object_token=written.object_token,
        ))
        expected.append(openevent_pb2.ObjectMetadata(
            name=name, type=content_type, description=description, nbytes=len(data),
        ))

    max_seq = client.get_status(principal=principal, token=token).max_seq
    response = client.batch_get_object_metadata(keys[index] for index in (1, 0, 1))
    assert list(response.metadata) == [expected[1], expected[0], expected[1]]
    assert response.metadata[1].description.encode("utf-8") == entries[0][2].encode("utf-8")
    assert list(client.batch_get_object_metadata([keys[0]]).metadata) == [expected[0]]
    assert client.get_status(principal=principal, token=token).max_seq == max_seq


def test_batch_object_metadata_rejects_invalid_batches_and_checks_every_token(
    admin: AdminClient, client: OpenEventClient
) -> None:
    principal = 1803
    token = _token(admin, principal)
    written = client.write_object(
        principal=principal, token=token, name="batch.bin",
        type="application/octet-stream", data=b"batch",
    )
    valid = openevent_pb2.ObjectKey(object_id=written.object_id, object_token=written.object_token)
    wrong_token = openevent_pb2.ObjectKey(object_id=written.object_id, object_token="wrong-token")
    missing = openevent_pb2.ObjectKey(object_id=(1 << 64) - 1, object_token=written.object_token)
    zero_id = openevent_pb2.ObjectKey(object_id=0, object_token=written.object_token)
    empty_token = openevent_pb2.ObjectKey(object_id=written.object_id, object_token="")
    invalid_batches = [
        ([], grpc.StatusCode.INVALID_ARGUMENT),
        ([valid] * 1025, grpc.StatusCode.INVALID_ARGUMENT),
        ([zero_id], grpc.StatusCode.INVALID_ARGUMENT),
        ([empty_token], grpc.StatusCode.INVALID_ARGUMENT),
        # Validate the complete batch shape before looking up any object.
        ([missing, zero_id], grpc.StatusCode.INVALID_ARGUMENT),
        ([wrong_token, empty_token], grpc.StatusCode.INVALID_ARGUMENT),
        ([valid, missing, valid], grpc.StatusCode.NOT_FOUND),
        # Neither duplicate position may bypass its own token check.
        ([valid, wrong_token, valid], grpc.StatusCode.NOT_FOUND),
        ([wrong_token, valid], grpc.StatusCode.NOT_FOUND),
    ]
    for keys, expected_code in invalid_batches:
        with pytest.raises(grpc.RpcError) as exc_info:
            client.batch_get_object_metadata(keys)
        _assert_rpc_code(exc_info, expected_code)

    assert len(client.batch_get_object_metadata([valid, valid]).metadata) == 2


def test_batch_object_metadata_accepts_1024_maximum_size_metadata_entries(
    admin: AdminClient, client: OpenEventClient
) -> None:
    principal = 1804
    token = _token(admin, principal)
    name = "名" * 85
    content_type = "型" * 85
    description = "🙂" * 1024
    assert len(name.encode("utf-8")) == 255
    assert len(content_type.encode("utf-8")) == 255
    assert len(description.encode("utf-8")) == 4096
    written = client.write_object(
        principal=principal, token=token, name=name, type=content_type,
        description=description, data=b"x",
    )
    key = openevent_pb2.ObjectKey(object_id=written.object_id, object_token=written.object_token)
    response = client.batch_get_object_metadata([key] * 1024)
    expected = openevent_pb2.ObjectMetadata(
        name=name, type=content_type, description=description, nbytes=1,
    )
    assert len(response.metadata) == 1024
    assert all(metadata == expected for metadata in response.metadata)
    # The largest legal batch exceeds gRPC's default 4 MiB receive limit.
    assert response.ByteSize() > 4 * 1024 * 1024


def test_message_object_references(admin: AdminClient, client: OpenEventClient) -> None:
    principal = 1901
    reader = 1902
    token = _token(admin, principal)
    reader_token = _token(admin, reader)
    channel_id = client.create_channel(
        principal=principal,
        token=token,
        name=_unique("sdk-e2e-object-references"),
        visibility=openevent_pb2.VISIBILITY_PUBLIC,
    ).channel.channel_id
    written = client.write_object(
        principal=principal,
        token=token,
        name="referenced",
        type="application/octet-stream",
        data=b"transferable capability",
    )
    key = openevent_pb2.ObjectKey(
        object_id=written.object_id, object_token=written.object_token
    )
    published = client.publish_auto_seq(
        principal=principal,
        token=token,
        channel_id=channel_id,
        payload=b"object references",
        object_keys=[key, key],
        uuid=_uuid(client),
    )

    fetched = client.fetch(
        principal=reader,
        token=reader_token,
        from_seq=published.seq,
        limit=1,
        channels=[channel_id],
    )
    assert len(fetched.messages) == 1
    assert [item.object_id for item in fetched.messages[0].object_keys] == [
        written.object_id,
        written.object_id,
    ]
    stream = client.subscribe(
        principal=reader,
        token=reader_token,
        from_seq=published.seq,
        channels=[channel_id],
    )
    subscribed = next(stream)
    stream.cancel()
    assert [item.object_id for item in subscribed.message.object_keys] == [
        written.object_id,
        written.object_id,
    ]
    listed = admin.list_messages(from_seq=published.seq, limit=1)
    assert len(listed.messages) == 1
    assert len(listed.messages[0].object_keys) == 2

    transferred_key = fetched.messages[0].object_keys[0]
    transferred = client.read_object(
        object_id=transferred_key.object_id,
        object_token=transferred_key.object_token,
        offset=0,
        nbytes=1024,
    )
    assert transferred.data == b"transferable capability"

    exactly_1024 = [key] * 1024
    accepted = client.publish_auto_seq(
        principal=principal,
        token=token,
        channel_id=channel_id,
        payload=b"",
        object_keys=exactly_1024,
        uuid=_uuid(client),
    )
    watermark = client.get_status(principal=principal, token=token).max_seq
    assert watermark == accepted.seq

    with pytest.raises(grpc.RpcError) as exc_info:
        client.publish_auto_seq(
            principal=principal,
            token=token,
            channel_id=channel_id,
            payload=b"",
            object_keys=[key] * 1025,
            uuid=_uuid(client),
        )
    _assert_rpc_code(exc_info, grpc.StatusCode.INVALID_ARGUMENT)
    assert client.get_status(principal=principal, token=token).max_seq == watermark

    invalid_key = openevent_pb2.ObjectKey(
        object_id=written.object_id, object_token="wrong-token"
    )
    with pytest.raises(grpc.RpcError) as exc_info:
        client.publish_auto_seq(
            principal=principal,
            token=token,
            channel_id=channel_id,
            payload=b"",
            object_keys=[key, invalid_key, key],
            uuid=_uuid(client),
        )
    _assert_rpc_code(exc_info, grpc.StatusCode.NOT_FOUND)
    assert client.get_status(principal=principal, token=token).max_seq == watermark


def test_object_response_loss_does_not_trigger_sdk_retry(
    admin: AdminClient, client: OpenEventClient
) -> None:
    principal = 2001
    token = _token(admin, principal)
    with _commit_then_abort_object_client() as (proxy, service):
        with pytest.raises(grpc.RpcError) as exc_info:
            proxy.write_object(
                principal=principal,
                token=token,
                name="lost-response.bin",
                type="application/octet-stream",
                data=b"committed despite lost response",
            )
        _assert_rpc_code(exc_info, grpc.StatusCode.UNAVAILABLE)
        assert service.write_calls == 1
        assert service.committed_response is not None
        committed = service.committed_response

    metadata = client.get_object_metadata(committed.object_id, committed.object_token)
    data = client.read_object(committed.object_id, committed.object_token, 0, metadata.nbytes)
    assert metadata.name == "lost-response.bin"
    assert data.data == b"committed despite lost response"


def test_system_initialization_and_zero_start(admin, client):
    token = _token(admin, 2001)
    system = client.get_channel(2001, token, 0).channel
    assert (system.channel_id, system.name, system.protocol, system.description) == (0, "system", "system.v1", "")
    assert system.visibility == openevent_pb2.VISIBILITY_PROTECTED
    assert not system.HasField("creator") and not system.members
    assert client.get_seq_by_uuid(0) == 0
    status = client.get_status(2001, token)
    assert status.max_seq == 0
    assert [c.channel_id for c in client.list_channels(2001, token).channels] == [0]
    for mode in (openevent_pb2.CHANNEL_FILTER_JOINED, openevent_pb2.CHANNEL_FILTER_OWNED):
        assert not client.list_channels(2001, token, filter=mode).channels
    message = admin.list_messages(0, 1).messages[0]
    assert message.seq == message.channel_id == message.principal == message.uuid == 0
    assert not message.recipients and not message.object_keys
    assert json.loads(message.payload) == {
        "kind": "system.initialization", "data": {"schema_version": 1},
    }
    assert client.fetch(2001, token, 0, 1).messages[0] == message
    assert not client.fetch(2001, token, 0, 1, only_my_recipient=True).messages
    channel = client.create_channel(2001, token, _unique("filtered-system")).channel.channel_id
    filtered = client.fetch(2001, token, 0, 1, channels=[channel]).messages
    assert len(filtered) == 1 and filtered[0].channel_id == channel and filtered[0].seq > 0
    stream = client.subscribe(2001, token)
    try:
        stream.wait_started(5_000)
        assert next(stream).message == message
    finally:
        stream.cancel()


def test_reserved_principals_and_system_mutations(admin, client):
    token = _token(admin, 2002)
    channel = client.create_channel(2002, token, _unique("reserved")).channel.channel_id
    before = client.get_status(2002, token).max_seq
    invalid = [
        lambda: admin.add_token(0), lambda: admin.delete_token(2002, ""),
        lambda: admin.delete_token(0, token), lambda: admin.delete_token(0, ""),
        lambda: client.get_status(0, token), lambda: client.fetch(0, token, 0, 1),
        lambda: client.get_channel(0, token, 0), lambda: client.list_channels(0, token),
        lambda: client.create_channel(0, token, "zero"),
        lambda: client.create_channel(2002, token, "zero", members=[0]),
        lambda: client.add_member(2002, token, channel, 0),
        lambda: client.remove_member(2002, token, channel, 0),
        lambda: client.write_object(0, token, "name", "type", b"x"),
        lambda: client.publish_auto_seq(0, token, channel, b"x", client.get_uuid()),
        lambda: client.publish_auto_seq(2002, token, channel, b"x", client.get_uuid(), recipients=[0]),
        lambda: client.publish(2002, token, channel, 1, b"x", client.get_uuid(), recipients=[0]),
        lambda: client.publish_auto_seq(2002, token, channel, b"x", 0),
    ]
    for call in invalid:
        with pytest.raises(grpc.RpcError) as error:
            call()
        assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    for call in (
        lambda: client.publish_auto_seq(2002, token, 0, b"x", client.get_uuid()),
        lambda: client.add_member(2002, token, 0, 2003),
        lambda: client.remove_member(2002, token, 0, 2003),
    ):
        with pytest.raises(grpc.RpcError) as error:
            call()
        assert error.value.code() == grpc.StatusCode.PERMISSION_DENIED
    assert client.get_status(2002, token).max_seq == before
    admin.delete_token(2002, "absent-token")
    admin.delete_token(2002, "absent-token")


@pytest.mark.parametrize("visibility", [0, 1, 2])
def test_remove_member_write_order_and_duplicate_uuid(admin, client, visibility):
    owner, member = 2101, 2102
    owner_token, member_token = _token(admin, owner), _token(admin, member)
    channel = client.create_channel(owner, owner_token, _unique("removal"), visibility=visibility, members=[member]).channel.channel_id
    used = client.get_uuid()
    seq = client.publish_auto_seq(member, member_token, channel, b"before", used).seq
    client.remove_member(owner, owner_token, channel, member)
    # A consumed UUID wins even if the writer's membership has since changed.
    with pytest.raises(grpc.RpcError) as error:
        client.publish_auto_seq(member, member_token, channel, b"changed", used)
    assert error.value.code() == grpc.StatusCode.ALREADY_EXISTS
    assert client.get_seq_by_uuid(used) == seq
    if visibility == openevent_pb2.VISIBILITY_PUBLIC:
        client.publish_auto_seq(member, member_token, channel, b"after", client.get_uuid())
    else:
        with pytest.raises(grpc.RpcError) as error:
            client.publish_auto_seq(member, member_token, channel, b"after", client.get_uuid())
        assert error.value.code() == grpc.StatusCode.PERMISSION_DENIED
    if visibility == openevent_pb2.VISIBILITY_PRIVATE:
        for call in (
            lambda: client.get_channel(member, member_token, channel),
            lambda: client.fetch(member, member_token, seq, 10, channels=[channel]),
        ):
            with pytest.raises(grpc.RpcError) as error:
                call()
            assert error.value.code() == grpc.StatusCode.PERMISSION_DENIED
    else:
        assert client.fetch(member, member_token, seq, 1, channels=[channel]).messages[0].payload == b"before"
    assert client.fetch(owner, owner_token, seq, 1).messages[0].payload == b"before"


@pytest.mark.parametrize("visibility,explicit", [(1, True), (2, True), (2, False)])
def test_subscription_observes_member_removal_and_token_deletion(admin, client, visibility, explicit):
    owner, member = 2201, 2202
    owner_token, member_token = _token(admin, owner), _token(admin, member)
    private = client.create_channel(owner, owner_token, _unique("membership"), visibility=visibility, members=[member]).channel.channel_id
    public = client.create_channel(owner, owner_token, _unique("public")).channel.channel_id
    start = client.publish_auto_seq(owner, owner_token, private, b"before", client.get_uuid()).seq
    stream = client.subscribe(member, member_token, from_seq=start, channels=[private] if explicit else [])
    try:
        stream.wait_started(5_000)
        assert next(stream).message.seq == start
        client.remove_member(owner, owner_token, private, member)
        if visibility == 2 and explicit:
            with pytest.raises(grpc.RpcError) as error:
                next(stream)
            assert error.value.code() == grpc.StatusCode.PERMISSION_DENIED
            return
        if visibility == openevent_pb2.VISIBILITY_PROTECTED:
            removed = next(stream).message
            assert _assert_system_message(removed, private, "system.member_removed") == {
                "schema_version": 1, "member": member,
            }
        private_seq = client.publish_auto_seq(owner, owner_token, private, b"after-private", client.get_uuid()).seq
        public_seq = client.publish_auto_seq(owner, owner_token, public, b"after-public", client.get_uuid()).seq
        assert next(stream).message.seq == (private_seq if visibility == 1 else public_seq)
        admin.delete_token(member, member_token)
        with pytest.raises(grpc.RpcError) as error:
            next(stream)
        assert error.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        stream.cancel()


def test_channel_creation_parameter_limits(admin, client):
    token = _token(admin, 2301)
    before = client.get_status(2301, token).max_seq
    with pytest.raises(grpc.RpcError) as error:
        client.create_channel(2301, token, "too-many-members", members=range(3000, 4024))
    assert error.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
    assert client.get_status(2301, token).max_seq == before

    # The creator counts toward the total, whether supplied or added automatically.
    channel = client.create_channel(2301, token, "n" * 255, protocol="p" * 255,
        description="d" * 4096, members=range(3000, 4023)).channel
    assert len(channel.members) == 1024 and list(channel.members).count(2301) == 1
    explicit_creator = client.create_channel(2301, token, "explicit-creator",
        members=[2301, *range(3000, 4023)]).channel
    assert len(explicit_creator.members) == 1024 and list(explicit_creator.members).count(2301) == 1

    before = client.get_status(2301, token).max_seq
    for member, expected_code in ((5000, grpc.StatusCode.RESOURCE_EXHAUSTED),
                                  (3000, grpc.StatusCode.ALREADY_EXISTS),
                                  (2301, grpc.StatusCode.ALREADY_EXISTS)):
        with pytest.raises(grpc.RpcError) as error:
            client.add_member(2301, token, channel.channel_id, member)
        assert error.value.code() == expected_code
        assert client.get_status(2301, token).max_seq == before
    assert client.get_channel(2301, token, channel.channel_id).channel == channel

    client.remove_member(2301, token, channel.channel_id, 3000)
    client.add_member(2301, token, channel.channel_id, 5000)
    replaced = client.get_channel(2301, token, channel.channel_id).channel
    assert len(replaced.members) == 1024 and 3000 not in replaced.members and 5000 in replaced.members
    duplicates = client.create_channel(2301, token, "duplicates", members=[2301] + [2302] * 1023).channel
    assert list(duplicates.members) == [2301, 2302]
    before = client.get_status(2301, token).max_seq
    for fields in ({"name": ""}, {"name": "n" * 256}, {"name": "名" * 86},
                   {"protocol": "p" * 256}, {"description": "d" * 4097}, {"members": [2301] * 1025}):
        arguments = {"name": "valid", **fields}
        with pytest.raises(grpc.RpcError) as error:
            client.create_channel(2301, token, **arguments)
        assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        assert client.get_status(2301, token).max_seq == before


@pytest.mark.parametrize("large", [False, True])
def test_message_pagination_preserves_all_payloads(admin, client, large):
    token = _token(admin, 2401)
    channel = client.create_channel(2401, token, _unique("pages")).channel.channel_id
    payloads = ([bytes([n]) * (size * 1024 * 1024) for n, size in enumerate([10, 10, 20])]
                if large else [f"message-{n}".encode() for n in range(7)])
    expected = [(client.publish_auto_seq(2401, token, channel, payload, client.get_uuid()).seq, payload)
                for payload in payloads]
    limit = 100 if large else 2
    for reader in (lambda cursor: client.fetch(2401, token, cursor, limit, channels=[channel]),
                   lambda cursor: admin.list_messages(cursor, limit)):
        cursor, actual = expected[0][0], []
        while True:
            page = reader(cursor)
            assert page.next_seq > cursor
            assert len(page.messages) <= limit
            if large:
                assert len(page.messages) == 1
            actual.extend((message.seq, message.payload) for message in page.messages)
            cursor = page.next_seq
            if cursor > page.last_seq:
                break
        assert actual == expected
        page = reader(cursor + 100)
        assert not page.messages and page.next_seq == page.last_seq + 1


def test_disconnect_and_inclusive_resume(admin, client):
    token = _token(admin, 2501)
    channel = client.create_channel(2501, token, _unique("resume")).channel.channel_id
    first = client.publish_auto_seq(2501, token, channel, b"one", client.get_uuid()).seq
    relay = _TcpRelay(_event_target())
    with OpenEventClient(relay.target) as proxied:
        stream = proxied.subscribe(2501, token, from_seq=first)
        try:
            assert next(stream).message.seq == first
            relay.close()
            with pytest.raises(grpc.RpcError):
                next(stream)
        finally:
            stream.cancel()
            relay.close()
    inclusive = client.subscribe(2501, token, from_seq=first)
    try:
        assert next(inclusive).message.seq == first
    finally:
        inclusive.cancel()
    second = client.publish_auto_seq(2501, token, channel, b"two", client.get_uuid()).seq
    resumed = client.subscribe(2501, token, from_seq=first + 1)
    try:
        assert next(resumed).message.seq == second
    finally:
        resumed.cancel()


def test_filtered_pagination_reaches_later_visible_messages(admin, client):
    token = _token(admin, 2601)
    selected = client.create_channel(2601, token, _unique("selected")).channel.channel_id
    other = client.create_channel(2601, token, _unique("other")).channel.channel_id
    expected = []
    for index in range(12):
        channel = selected if index in (0, 4, 11) else other
        recipients = [2601] if index != 4 else []
        payload = str(index).encode()
        seq = client.publish_auto_seq(2601, token, channel, payload, client.get_uuid(), recipients=recipients).seq
        if channel == selected and recipients:
            expected.append((seq, payload))
    cursor, actual = 0, []
    while True:
        page = client.fetch(2601, token, cursor, 1, channels=[selected], only_my_recipient=True)
        assert page.next_seq > cursor
        actual.extend((message.seq, message.payload) for message in page.messages)
        cursor = page.next_seq
        if cursor > page.last_seq:
            break
    assert actual == expected


def test_wait_started_distinguishes_acceptance_from_rejection(admin, client):
    token = _token(admin, 2701)
    owner_token = _token(admin, 2702)
    private_channel = client.create_channel(
        2702, owner_token, name=_unique("sdk-e2e-rejected-subscription"),
        visibility=openevent_pb2.VISIBILITY_PRIVATE,
    ).channel.channel_id
    too_far = client.get_status(2701, token).max_seq + 2
    for fields, expected in [
        ({"principal": 0, "token": token}, grpc.StatusCode.INVALID_ARGUMENT),
        ({"principal": 2701, "token": "wrong"}, grpc.StatusCode.UNAUTHENTICATED),
        ({"principal": 2701, "token": token, "from_seq": too_far}, grpc.StatusCode.OUT_OF_RANGE),
        ({"principal": 2701, "token": token, "channels": [999]}, grpc.StatusCode.NOT_FOUND),
        (
            {"principal": 2701, "token": token, "channels": [private_channel]},
            grpc.StatusCode.PERMISSION_DENIED,
        ),
    ]:
        stream = client.subscribe(**fields)
        try:
            with pytest.raises(grpc.RpcError) as error:
                stream.wait_started(5_000)
            assert error.value.code() == expected
            assert ("openevent-subscription", "accepted") not in stream.initial_metadata()
        finally:
            stream.cancel()
    stream = client.subscribe(2701, token, only_my_recipient=True)
    try:
        # Receive acceptance directly so wait_started is first called after stream termination.
        accepted_metadata = stream.initial_metadata()
        assert ("openevent-subscription", "accepted") in accepted_metadata
        ended = Event()
        stream.add_done_callback(lambda _: ended.set())
        admin.delete_token(2701, token)
        assert ended.wait(5)
        assert stream.wait_started(5_000) is None
        assert stream.initial_metadata() == accepted_metadata
        with pytest.raises(grpc.RpcError) as error:
            next(stream)
        assert error.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        stream.cancel()


def test_channel_events_share_sequences_and_uuid_allocation(admin, client):
    """E2E-SDK-011: successful mutations persist one event, no-ops consume nothing."""
    owner, member, added = 2801, 2802, 2803
    token = _token(admin, owner)

    def allocate_one():
        return client.event_stub.AllocateUuids(
            openevent_pb2.AllocateUuidsRequest(count=1), timeout=5,
        ).uuids[0]

    reserved = [allocate_one()]
    before = client.get_status(owner, token).max_seq
    channel = client.create_channel(
        owner, token, '会话 "quoted"\nname', protocol="chat.v1",
        description='line one\nline two \\ "quoted"',
        visibility=openevent_pb2.VISIBILITY_PRIVATE, members=[owner, member, member],
    ).channel
    assert set(channel.members) == {owner, member} and len(channel.members) == 2
    assert client.get_channel(owner, token, channel.channel_id).channel == channel
    assert client.get_status(owner, token).max_seq == before + 1
    initialized = client.fetch(owner, token, before + 1, 10).messages
    assert len(initialized) == 1
    _assert_channel_initialized(initialized[0], channel)

    reserved.append(allocate_one())
    client.add_member(owner, token, channel.channel_id, added)
    assert client.get_status(owner, token).max_seq == before + 2
    assert set(client.get_channel(owner, token, channel.channel_id).channel.members) == {
        owner, member, added,
    }
    joined = client.fetch(owner, token, before + 2, 10).messages
    assert len(joined) == 1
    assert _assert_system_message(joined[0], channel.channel_id, "system.member_added") == {
        "schema_version": 1, "member": added,
    }

    reserved.append(allocate_one())
    client.remove_member(owner, token, channel.channel_id, member)
    assert client.get_status(owner, token).max_seq == before + 3
    assert set(client.get_channel(owner, token, channel.channel_id).channel.members) == {owner, added}
    removed = client.fetch(owner, token, before + 3, 10).messages
    assert len(removed) == 1
    assert _assert_system_message(removed[0], channel.channel_id, "system.member_removed") == {
        "schema_version": 1, "member": member,
    }

    reserved.append(allocate_one())
    with pytest.raises(grpc.RpcError) as error:
        client.add_member(owner, token, channel.channel_id, added)
    assert error.value.code() == grpc.StatusCode.ALREADY_EXISTS
    client.remove_member(owner, token, channel.channel_id, member)
    assert client.get_status(owner, token).max_seq == before + 3
    reserved.append(allocate_one())
    assert reserved[-1] == reserved[-2] + 1

    events = list(client.fetch(owner, token, 0, 100, channels=[channel.channel_id]).messages)
    assert events == [initialized[0], joined[0], removed[0]]
    assert [message.seq for message in events] == list(range(before + 1, before + 4))
    assert len({message.uuid for message in events}) == 3
    assert {message.uuid for message in events}.isdisjoint(reserved)
    for message in events:
        assert client.get_seq_by_uuid(message.uuid) == message.seq

    # Previously allocated business UUIDs remain valid after system events use the allocator.
    published = []
    for uuid in reserved:
        seq = client.publish_auto_seq(owner, token, channel.channel_id, b"opaque business payload", uuid).seq
        published.append(seq)
        assert client.get_seq_by_uuid(uuid) == seq
    fetched = client.fetch(owner, token, 0, 100, channels=[channel.channel_id]).messages
    assert list(fetched[:3]) == events
    assert [message.seq for message in fetched[3:]] == published
    assert all(message.principal == owner and message.payload == b"opaque business payload"
               for message in fetched[3:])
    assert client.get_channel(owner, token, channel.channel_id).channel.protocol == "chat.v1"


def test_all_readable_subscription_discovers_new_channels_and_resumes(admin, client):
    """E2E-SDK-011: an already accepted stream discovers future private channels."""
    owner, agent = 2811, 2812
    token, agent_token = _token(admin, owner), _token(admin, agent)
    start = client.get_status(agent, agent_token).max_seq + 1
    stream = client.subscribe(agent, agent_token, from_seq=start, channels=[])
    try:
        stream.wait_started(5_000)
        channel = client.create_channel(
            owner, token, _unique("new-chat"), protocol="chat.v1",
            visibility=openevent_pb2.VISIBILITY_PRIVATE, members=[agent],
        ).channel
        published = client.publish_auto_seq(
            owner, token, channel.channel_id, b"first business message", client.get_uuid(),
        ).seq
        messages = _take_messages(stream, 2)
    finally:
        stream.cancel()
    _assert_channel_initialized(messages[0], channel)
    assert messages[0].seq == start and messages[1].seq == published == start + 1
    assert messages[1].principal == owner and messages[1].payload == b"first business message"
    assert list(client.fetch(agent, agent_token, start, 10).messages) == messages

    # A channel created while disconnected is discovered from the saved cursor too.
    later = client.create_channel(
        owner, token, _unique("offline-chat"), protocol="chat.v1",
        visibility=openevent_pb2.VISIBILITY_PRIVATE, members=[agent],
    ).channel
    later_seq = client.publish_auto_seq(
        owner, token, later.channel_id, b"while disconnected", client.get_uuid(),
    ).seq
    resumed = client.subscribe(agent, agent_token, from_seq=published + 1)
    restored = _take_messages(resumed, 2)
    _assert_channel_initialized(restored[0], later)
    assert restored[1].seq == later_seq and restored[1].payload == b"while disconnected"


def test_history_watermark_then_subscribe_keeps_channels_created_during_recovery(admin, client):
    """E2E-SDK-011: Fetch to H and Subscribe from H+1 need no channel enumeration."""
    owner, agent = 2821, 2822
    token, agent_token = _token(admin, owner), _token(admin, agent)
    old = client.create_channel(
        owner, token, _unique("history-chat"), protocol="chat.v1",
        visibility=openevent_pb2.VISIBILITY_PRIVATE, members=[agent],
    ).channel
    old_seq = client.publish_auto_seq(owner, token, old.channel_id, b"old", client.get_uuid()).seq
    watermark = client.get_status(agent, agent_token).max_seq
    first = client.fetch(agent, agent_token, from_seq=0, limit=1, channels=[])
    assert first.next_seq <= watermark

    created_during_recovery = client.create_channel(
        owner, token, _unique("during-recovery"), protocol="chat.v1",
        visibility=openevent_pb2.VISIBILITY_PRIVATE, members=[agent],
    ).channel
    new_seq = client.publish_auto_seq(
        owner, token, created_during_recovery.channel_id, b"new", client.get_uuid(),
    ).seq

    history, cursor = list(first.messages), first.next_seq
    saw_after_watermark = False
    while cursor <= watermark:
        page = client.fetch(agent, agent_token, cursor, 100, channels=[])
        assert page.next_seq > cursor
        saw_after_watermark |= any(message.seq > watermark for message in page.messages)
        history.extend(message for message in page.messages if message.seq <= watermark)
        cursor = page.next_seq
    assert saw_after_watermark, "the recovery page must include messages beyond its fixed watermark"
    assert history[0].seq == 0
    _assert_channel_initialized(history[1], old)
    assert history[2].seq == old_seq == watermark and history[2].payload == b"old"
    assert len(history) == 3

    live = _take_messages(client.subscribe(agent, agent_token, from_seq=watermark + 1), 2)
    _assert_channel_initialized(live[0], created_during_recovery)
    assert live[0].seq == watermark + 1
    assert live[1].seq == new_seq and live[1].payload == b"new"
    assert [message.seq for message in history + live] == list(range(new_seq + 1))


@pytest.mark.parametrize("visibility", [0, 1, 2])
def test_channel_system_events_follow_acl_and_recipient_filtering(admin, client, visibility):
    """E2E-SDK-011: readable does not imply membership; system events have no recipients."""
    owner, reader, member = 2831, 2832, 2833
    token, reader_token = _token(admin, owner), _token(admin, reader)
    channel = client.create_channel(
        owner, token, _unique("event-acl"), visibility=visibility,
        protocol="application/octet-stream",
    ).channel
    client.add_member(owner, token, channel.channel_id, member)
    client.remove_member(owner, token, channel.channel_id, member)
    own_events = client.fetch(owner, token, 0, 100, channels=[channel.channel_id]).messages
    assert len(own_events) == 3
    _assert_channel_initialized(own_events[0], channel)
    for message, kind in zip(own_events[1:], ("system.member_added", "system.member_removed")):
        assert _assert_system_message(message, channel.channel_id, kind) == {
            "schema_version": 1, "member": member,
        }
    assert not client.fetch(owner, token, 0, 100, only_my_recipient=True).messages

    all_visible = client.fetch(reader, reader_token, 0, 100, channels=[]).messages
    if visibility == openevent_pb2.VISIBILITY_PRIVATE:
        assert all(message.channel_id != channel.channel_id for message in all_visible)
        with pytest.raises(grpc.RpcError) as error:
            client.fetch(reader, reader_token, 0, 100, channels=[channel.channel_id])
        assert error.value.code() == grpc.StatusCode.PERMISSION_DENIED
    else:
        assert reader not in client.get_channel(reader, reader_token, channel.channel_id).channel.members
        assert [message for message in all_visible if message.channel_id == channel.channel_id] == list(own_events)
        stream = client.subscribe(reader, reader_token, from_seq=0, channels=[channel.channel_id])
        assert _take_messages(stream, 3) == list(own_events)

    # The next addressed business message is the first visible result, even when
    # subscription scanning starts before all three empty-recipient system events.
    stream = client.subscribe(owner, token, from_seq=0, only_my_recipient=True)
    try:
        stream.wait_started(5_000)
        seq = client.publish_auto_seq(
            owner, token, channel.channel_id, b"direct", client.get_uuid(), recipients=[owner],
        ).seq
        assert _take_messages(stream, 1)[0].seq == seq
    finally:
        stream.cancel()


def test_all_readable_subscription_receives_new_membership_without_old_history(admin, client):
    """E2E-SDK-011: new read access affects future batches, not the consumed prefix."""
    owner, agent = 2841, 2842
    token, agent_token = _token(admin, owner), _token(admin, agent)
    channel = client.create_channel(
        owner, token, _unique("join-existing"), protocol="chat.v1",
        visibility=openevent_pb2.VISIBILITY_PRIVATE,
    ).channel
    old_seq = client.publish_auto_seq(owner, token, channel.channel_id, b"old", client.get_uuid()).seq
    # The explicit starting cursor is past the inaccessible prefix, so the
    # result does not depend on how quickly a background batch scanned it.
    stream = client.subscribe(agent, agent_token, from_seq=old_seq + 1)
    try:
        stream.wait_started(5_000)
        client.add_member(owner, token, channel.channel_id, agent)
        new_seq = client.publish_auto_seq(owner, token, channel.channel_id, b"new", client.get_uuid()).seq
        messages = _take_messages(stream, 2)
    finally:
        stream.cancel()
    assert _assert_system_message(messages[0], channel.channel_id, "system.member_added") == {
        "schema_version": 1, "member": agent,
    }
    assert messages[0].seq == old_seq + 1 and messages[1].seq == new_seq
    assert messages[1].payload == b"new"
    current = client.get_channel(agent, agent_token, channel.channel_id).channel
    assert current.protocol == "chat.v1" and agent in current.members
    history = client.fetch(agent, agent_token, 0, 100, channels=[channel.channel_id]).messages
    _assert_channel_initialized(history[0], channel)
    assert history[1].seq == old_seq and history[1].payload == b"old"
