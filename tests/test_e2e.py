from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
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


@pytest.fixture(scope="session")
def admin() -> AdminClient:
    return AdminClient(_admin_target())


@pytest.fixture(scope="session")
def client() -> OpenEventClient:
    return OpenEventClient(_event_target())


def _token(admin: AdminClient, principal: int) -> str:
    return admin.add_token(target_principal=principal).binding.token


def _unique(prefix: str) -> str:
    return f"{prefix}-{time.time_ns()}"


def _assert_rpc_code(exc_info: pytest.ExceptionInfo[grpc.RpcError], code: grpc.StatusCode) -> None:
    assert exc_info.value.code() == code


def _messages_by_payload(messages: Iterable, payload: bytes):
    return [message for message in messages if message.payload == payload]


def _list_all_tokens(admin: AdminClient, limit: int = 100):
    bindings = []
    page_token = ""
    seen_page_tokens = set()
    while True:
        page = admin.list_tokens(page_token=page_token, limit=limit)
        assert len(page.bindings) <= limit
        bindings.extend(page.bindings)
        if not page.next_page_token:
            return bindings
        assert page.next_page_token not in seen_page_tokens
        seen_page_tokens.add(page.next_page_token)
        page_token = page.next_page_token


def _fetch_to_watermark(
    client: OpenEventClient,
    principal: int,
    token: str,
    from_seq: int,
    last_seq: int,
):
    messages = []
    next_seq = from_seq
    while next_seq <= last_seq:
        page = client.fetch(
            principal=principal,
            token=token,
            from_seq=next_seq,
            limit=1000,
        )
        messages.extend(message for message in page.messages if message.seq <= last_seq)
        assert page.next_seq > next_seq
        next_seq = page.next_seq
    return messages


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

    client.publish(principal=principal, token=token, channel_id=channel_id, seq=next_seq, payload=b"cas-ok")
    with pytest.raises(grpc.RpcError) as exc_info:
        client.publish(principal=principal, token=token, channel_id=channel_id, seq=next_seq, payload=b"cas-repeat")
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
        )
    _assert_rpc_code(exc_info, grpc.StatusCode.PERMISSION_DENIED)

    outsider_fetch = client.fetch(
        principal=outsider,
        token=outsider_token,
        from_seq=member_seq,
        limit=10,
        channels=[channel_id],
    )
    assert not _messages_by_payload(outsider_fetch.messages, b"member can write")
    assert outsider_fetch.next_seq > member_seq

    client.remove_member(principal=owner, token=owner_token, channel_id=channel_id, target_principal=member)
    with pytest.raises(grpc.RpcError) as exc_info:
        client.publish_auto_seq(
            principal=member,
            token=member_token,
            channel_id=channel_id,
            payload=b"former member blocked",
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
    ).seq
    client.publish_auto_seq(
        principal=owner,
        token=owner_token,
        channel_id=channel_id,
        payload=b"broadcast without recipients",
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
    ).seq
    second_seq = client.publish_auto_seq(
        principal=principal,
        token=token,
        channel_id=second_channel_id,
        payload=b"fetch second channel",
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
    ).seq
    history_stream = client.subscribe(principal=principal, token=token, from_seq=history_seq)
    first = next(history_stream)
    history_stream.cancel()
    assert first.message.seq == history_seq
    assert first.message.payload == b"subscribe history"

    future_stream = client.subscribe(principal=principal, token=token, from_seq=0)
    executor = ThreadPoolExecutor(max_workers=1)
    pending = executor.submit(next, future_stream)
    try:
        time.sleep(0.2)
        future_seq = client.publish_auto_seq(
            principal=principal,
            token=token,
            channel_id=channel_id,
            payload=b"subscribe future",
        ).seq
        future = pending.result(timeout=5)
        assert future.message.seq == future_seq
        assert future.message.payload == b"subscribe future"
    finally:
        future_stream.cancel()
        executor.shutdown(wait=True, cancel_futures=True)

    max_seq = client.get_status(principal=principal, token=token).max_seq
    boundary = list(client.subscribe(principal=principal, token=token, from_seq=max_seq + 10))
    assert len(boundary) == 1
    assert boundary[0].next_seq == max_seq + 1


def test_token_delete_changes_authentication(admin: AdminClient, client: OpenEventClient) -> None:
    principal = 1501
    token = _token(admin, principal)
    pagination_tokens = {_token(admin, 1502), _token(admin, 1503)}
    client.get_status(principal=principal, token=token)

    admin.delete_token(target_token=token)
    remaining = _list_all_tokens(admin, limit=1)

    with pytest.raises(grpc.RpcError) as exc_info:
        client.get_status(principal=principal, token=token)
    _assert_rpc_code(exc_info, grpc.StatusCode.UNAUTHENTICATED)
    remaining_tokens = {binding.token for binding in remaining}
    assert token not in remaining_tokens
    assert pagination_tokens.issubset(remaining_tokens)

    for pagination_token in pagination_tokens:
        admin.delete_token(target_token=pagination_token)


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
    ).seq
    private_seq = client.publish_auto_seq(
        principal=principal,
        token=token,
        channel_id=private_channel_id,
        payload=private_payload,
    ).seq
    recipient_seq = client.publish_auto_seq(
        principal=principal,
        token=token,
        channel_id=private_channel_id,
        recipients=[member],
        payload=recipient_payload,
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

    outsider_fetch = client.fetch(
        principal=outsider,
        token=outsider_token,
        from_seq=private_seq,
        limit=1000,
        channels=[private_channel_id],
    )
    assert private_payload not in {message.payload for message in outsider_fetch.messages}
    assert recipient_payload not in {message.payload for message in outsider_fetch.messages}

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


def test_uncertain_publish_result_is_reconciled_without_direct_retry(
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
    auto_payload = _unique("uncertain-auto").encode()
    with _commit_then_abort_client() as proxy:
        with pytest.raises(grpc.RpcError) as exc_info:
            proxy.publish_auto_seq(
                principal=principal,
                token=token,
                channel_id=channel_id,
                payload=auto_payload,
            )
        _assert_rpc_code(exc_info, grpc.StatusCode.UNAVAILABLE)
    auto_watermark = client.get_status(principal=principal, token=token).max_seq
    auto_matches = _messages_by_payload(
        _fetch_to_watermark(client, principal, token, auto_before + 1, auto_watermark),
        auto_payload,
    )
    assert len(auto_matches) == 1

    cas_before = client.get_status(principal=principal, token=token).max_seq
    cas_seq = cas_before + 1
    cas_payload = _unique("uncertain-cas").encode()
    with _commit_then_abort_client() as proxy:
        with pytest.raises(grpc.RpcError) as exc_info:
            proxy.publish(
                principal=principal,
                token=token,
                channel_id=channel_id,
                seq=cas_seq,
                payload=cas_payload,
            )
        _assert_rpc_code(exc_info, grpc.StatusCode.UNAVAILABLE)
    cas_watermark = client.get_status(principal=principal, token=token).max_seq
    cas_matches = _messages_by_payload(
        _fetch_to_watermark(client, principal, token, cas_seq, cas_watermark),
        cas_payload,
    )
    assert len(cas_matches) == 1
    assert cas_matches[0].seq == cas_seq
