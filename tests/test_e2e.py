from __future__ import annotations

import os
import time
from typing import Iterable

import grpc
import pytest

from openevent.sdk import AdminClient, OpenEventClient
from openevent.sdk.proto import openevent_pb2


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

    max_seq = client.get_status(principal=principal, token=token).max_seq
    boundary = list(client.subscribe(principal=principal, token=token, from_seq=max_seq + 10))
    assert len(boundary) == 1
    assert boundary[0].next_seq == max_seq + 1


def test_token_delete_changes_authentication(admin: AdminClient, client: OpenEventClient) -> None:
    principal = 1501
    token = _token(admin, principal)
    client.get_status(principal=principal, token=token)

    admin.delete_token(target_token=token)
    remaining = admin.list_tokens().bindings

    with pytest.raises(grpc.RpcError) as exc_info:
        client.get_status(principal=principal, token=token)
    _assert_rpc_code(exc_info, grpc.StatusCode.UNAUTHENTICATED)
    assert token not in {binding.token for binding in remaining}
