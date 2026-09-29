"""Subscription acceptance must be distinct from ordinary gRPC response headers."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread

import grpc

from openevent.sdk import OpenEventClient
from openevent.sdk.proto import openevent_pb2_grpc


def test_empty_initial_metadata_waits_for_the_actual_rejection(monkeypatch):
    reject = Event()
    waiting_for_status = Event()
    finished = Event()
    errors = []

    class Service(openevent_pb2_grpc.EventServiceServicer):
        def Subscribe(self, request, context):
            context.send_initial_metadata(())
            assert reject.wait(5)
            context.abort(grpc.StatusCode.UNAVAILABLE, "server is shutting down")
            yield

    with ThreadPoolExecutor(max_workers=1) as executor:
        server = grpc.server(executor)
        openevent_pb2_grpc.add_EventServiceServicer_to_server(Service(), server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        try:
            with OpenEventClient(f"127.0.0.1:{port}") as client:
                stream = client.subscribe(1001, "valid")
                assert stream.initial_metadata() == ()
                original_code = stream._stream.code

                def observe_code():
                    waiting_for_status.set()
                    return original_code()

                monkeypatch.setattr(stream._stream, "code", observe_code)

                def wait_started():
                    try:
                        stream.wait_started(5_000)
                    except Exception as error:
                        errors.append(error)
                    finally:
                        finished.set()

                waiter = Thread(target=wait_started)
                waiter.start()
                try:
                    assert waiting_for_status.wait(2)
                    assert not finished.is_set(), "empty headers were mistaken for acceptance"
                    reject.set()
                    assert finished.wait(2)
                    assert len(errors) == 1 and errors[0] is stream._stream
                    assert errors[0].code() == grpc.StatusCode.UNAVAILABLE
                finally:
                    reject.set()
                    stream.cancel()
                    waiter.join(5)
                    assert not waiter.is_alive()
        finally:
            reject.set()
            server.stop(0).wait(5)
