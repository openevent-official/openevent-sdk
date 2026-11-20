from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openevent.sdk import OpenEventClient
from openevent.sdk import client as client_module
from openevent.sdk.proto import openevent_pb2


@pytest.mark.parametrize(
    "timeout_ms",
    [None, True, 0, float("nan"), 2_147_483_648, 10**400],
)
def test_client_rejects_invalid_timeout_ms(timeout_ms) -> None:
    with pytest.raises(ValueError):
        OpenEventClient("unused", timeout_ms=timeout_ms)


def test_close_cancels_pending_uuid_allocation_before_waiting_for_its_lock(monkeypatch) -> None:
    allocation_started = Event()
    channel_closed = Event()

    class Stub:
        def AllocateUuids(self, request, timeout):
            allocation_started.set()
            channel_closed.wait()
            raise client_module.grpc.RpcError("allocation cancelled")

    channel = Mock(spec=client_module.grpc.Channel)
    channel.close.side_effect = channel_closed.set
    monkeypatch.setattr(client_module.grpc, "insecure_channel", lambda target, options: channel)
    with OpenEventClient("unused") as client, ThreadPoolExecutor(max_workers=2) as pool:
        client.event_stub = Stub()
        allocation = pool.submit(client.get_uuid)
        try:
            assert allocation_started.wait(2)
            closing = pool.submit(client.close)
            with pytest.raises(client_module.grpc.RpcError):
                allocation.result(timeout=2)
            closing.result(timeout=2)
        finally:
            channel_closed.set()


def _subscribe_to_call(call):
    client = OpenEventClient.__new__(OpenEventClient)
    client.event_stub = SimpleNamespace(Subscribe=lambda request, timeout: call)
    return client.subscribe(principal=7, token="token")


def test_subscribe_wait_started_cancels_on_timeout() -> None:
    release = Event()

    class PendingStream(client_module.grpc.RpcError):
        def initial_metadata(self):
            assert release.wait(2), "wait_started did not cancel the subscription on timeout"
            return ()

        def code(self):
            return client_module.grpc.StatusCode.CANCELLED

        def cancel(self):
            release.set()
            return True

    pending = _subscribe_to_call(PendingStream())
    try:
        with pytest.raises(TimeoutError):
            pending.wait_started(10)
        assert release.is_set()
    finally:
        release.set()


def test_empty_initial_metadata_waits_for_the_actual_rejection() -> None:
    waiting_for_status = Event()
    reject = Event()

    class DelayedRejection(client_module.grpc.RpcError):
        def initial_metadata(self):
            return ()

        def code(self):
            waiting_for_status.set()
            reject.wait()
            return client_module.grpc.StatusCode.UNAVAILABLE

    call = DelayedRejection()
    stream = _subscribe_to_call(call)
    with ThreadPoolExecutor(max_workers=1) as executor:
        waiter = executor.submit(stream.wait_started, 5_000)
        try:
            assert waiting_for_status.wait(2)
            assert not waiter.done(), "empty response headers were mistaken for acceptance"
            reject.set()
            with pytest.raises(client_module.grpc.RpcError) as error:
                waiter.result(timeout=2)
            assert error.value is call
        finally:
            reject.set()


def test_uuid_pool_refills_in_order():
    batch_starts = iter((1, 4097))

    class Stub:
        def AllocateUuids(self, request, timeout):
            start = next(batch_starts)
            return openevent_pb2.AllocateUuidsResponse(uuids=range(start, start + request.count))

    with OpenEventClient("127.0.0.1:1") as client:
        client.event_stub = Stub()
        assert [client.get_uuid() for _ in range(1024)] == list(range(1, 1025))
        assert client.get_uuid() == 4097
        assert client.get_uuid() == 4098
