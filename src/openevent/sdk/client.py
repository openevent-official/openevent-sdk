from __future__ import annotations

import math
from collections import deque
from numbers import Real
from threading import Event, Lock, Thread
from typing import Iterable

import grpc

try:
    from .proto import openevent_pb2, openevent_pb2_grpc
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "generated protobuf modules are missing; run make build from openevent-sdk"
    ) from exc


_DEFAULT_MAX_GRPC_MESSAGE_BYTES = 64 * 1024 * 1024
_DEFAULT_RPC_TIMEOUT_MS = 30_000.0
_SUBSCRIBE_KEEPALIVE_TIME_MS = 30_000
_SUBSCRIBE_KEEPALIVE_TIMEOUT_MS = 10_000
_UUID_POOL_BATCH_SIZE = 1024
_DEFAULT_CHANNEL_OPTIONS = (
    ("grpc.max_send_message_length", _DEFAULT_MAX_GRPC_MESSAGE_BYTES),
    ("grpc.max_receive_message_length", _DEFAULT_MAX_GRPC_MESSAGE_BYTES),
    ("grpc.keepalive_time_ms", _SUBSCRIBE_KEEPALIVE_TIME_MS),
    ("grpc.keepalive_timeout_ms", _SUBSCRIBE_KEEPALIVE_TIMEOUT_MS),
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.http2.max_pings_without_data", 0),
)


def _validate_timeout_ms(timeout_ms: object) -> float:
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, Real):
        raise ValueError("timeout_ms must be a finite number greater than 0")
    value = float(timeout_ms)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout_ms must be a finite number greater than 0")
    return value


class _DeduplicatingSubscribeStream:
    def __init__(self, stream):
        self._stream = stream
        self._last_seq = None
        self._started = Event()
        self._started_lock = Lock()
        self._started_waiter_started = False
        self._started_error = None

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            item = next(self._stream)
            if item.HasField("message"):
                seq = item.message.seq
                if self._last_seq is not None and seq <= self._last_seq:
                    continue
                self._last_seq = seq
            return item

    def wait_started(self, timeout_ms: object) -> None:
        """Wait up to timeout_ms for server acceptance without setting a stream deadline."""
        timeout_seconds = _validate_timeout_ms(timeout_ms) / 1000.0
        with self._started_lock:
            if not self._started_waiter_started:
                self._started_waiter_started = True
                Thread(target=self._await_initial_metadata, daemon=True).start()

        if not self._started.wait(timeout_seconds):
            self.cancel()
            raise TimeoutError("Subscribe did not receive server initial metadata before timeout")
        if self._started_error is not None:
            raise self._started_error

    def _await_initial_metadata(self) -> None:
        try:
            metadata = self._stream.initial_metadata()
            if not metadata and hasattr(self._stream, "done") and self._stream.done():
                if self._stream.code() != grpc.StatusCode.OK:
                    raise self._stream
        except Exception as exc:
            self._started_error = exc
        finally:
            self._started.set()

    def __getattr__(self, name):
        return getattr(self._stream, name)


class OpenEventClient:
    def __init__(
        self,
        target: str,
        timeout_ms: float = _DEFAULT_RPC_TIMEOUT_MS,
    ):
        self._closed = True
        self._uuid_pool = deque()
        self._uuid_pool_lock = Lock()
        self.timeout_ms = timeout_ms
        self.channel = grpc.insecure_channel(target, options=_DEFAULT_CHANNEL_OPTIONS)
        self._closed = False
        try:
            self.event_stub = openevent_pb2_grpc.EventServiceStub(self.channel)
            self.object_stub = openevent_pb2_grpc.ObjectStorageServiceStub(self.channel)
            self.channel_stub = openevent_pb2_grpc.ChannelServiceStub(self.channel)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        with self._uuid_pool_lock:
            self._uuid_pool.clear()
        if hasattr(self, "channel"):
            self.channel.close()

    def __enter__(self) -> OpenEventClient:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.close()
        return False

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    @property
    def timeout_ms(self) -> float:
        return self._timeout_ms

    @timeout_ms.setter
    def timeout_ms(self, value: object) -> None:
        self._timeout_ms = _validate_timeout_ms(value)

    @property
    def _rpc_timeout_seconds(self) -> float:
        return self._timeout_ms / 1000.0

    def get_status(self, principal: int, token: str):
        return self.event_stub.GetStatus(
            openevent_pb2.GetStatusRequest(principal=principal, token=token),
            timeout=self._rpc_timeout_seconds,
        )

    def get_uuid(self) -> int:
        with self._uuid_pool_lock:
            if not self._uuid_pool:
                response = self.event_stub.AllocateUuids(
                    openevent_pb2.AllocateUuidsRequest(count=_UUID_POOL_BATCH_SIZE),
                    timeout=self._rpc_timeout_seconds,
                )
                if len(response.uuids) != _UUID_POOL_BATCH_SIZE:
                    raise RuntimeError("server returned an incomplete UUID allocation batch")
                self._uuid_pool.extend(response.uuids)
            return self._uuid_pool.popleft()

    def get_seq_by_uuid(self, uuid: int) -> int:
        response = self.event_stub.GetSeqByUuid(
            openevent_pb2.GetSeqByUuidRequest(uuid=uuid),
            timeout=self._rpc_timeout_seconds,
        )
        return response.seq

    def publish(
        self,
        principal: int,
        token: str,
        channel_id: int,
        seq: int,
        payload: bytes,
        uuid: int,
        recipients: Iterable[int] = (),
        object_keys: Iterable[openevent_pb2.ObjectKey] = (),
    ):
        return self.event_stub.Publish(
            openevent_pb2.PublishRequest(
                principal=principal,
                token=token,
                channel_id=channel_id,
                seq=seq,
                recipients=list(recipients),
                payload=payload,
                object_keys=list(object_keys),
                uuid=uuid,
            ),
            timeout=self._rpc_timeout_seconds,
        )

    def publish_auto_seq(
        self,
        principal: int,
        token: str,
        channel_id: int,
        payload: bytes,
        uuid: int,
        recipients: Iterable[int] = (),
        object_keys: Iterable[openevent_pb2.ObjectKey] = (),
    ):
        return self.event_stub.PublishAutoSeq(
            openevent_pb2.PublishAutoSeqRequest(
                principal=principal,
                token=token,
                channel_id=channel_id,
                recipients=list(recipients),
                payload=payload,
                object_keys=list(object_keys),
                uuid=uuid,
            ),
            timeout=self._rpc_timeout_seconds,
        )

    def fetch(
        self,
        principal: int,
        token: str,
        from_seq: int,
        limit: int,
        only_my_recipient: bool = False,
        channels: Iterable[int] = (),
    ):
        return self.event_stub.Fetch(
            openevent_pb2.FetchRequest(
                principal=principal,
                token=token,
                from_seq=from_seq,
                limit=limit,
                channels=list(channels),
                only_my_recipient=only_my_recipient,
            ),
            timeout=self._rpc_timeout_seconds,
        )

    def subscribe(
        self,
        principal: int,
        token: str,
        from_seq: int = 0,
        only_my_recipient: bool = False,
        channels: Iterable[int] = (),
    ):
        stream = self.event_stub.Subscribe(
            openevent_pb2.SubscribeRequest(
                principal=principal,
                token=token,
                from_seq=from_seq,
                only_my_recipient=only_my_recipient,
                channels=list(channels),
            ),
            timeout=None,
        )
        return _DeduplicatingSubscribeStream(stream)

    def write_object(
        self,
        principal: int,
        token: str,
        name: str,
        type: str,
        data: bytes,
        description: str = "",
    ):
        return self.object_stub.WriteObject(
            openevent_pb2.WriteObjectRequest(
                principal=principal,
                token=token,
                name=name,
                type=type,
                description=description,
                data=data,
            ),
            timeout=self._rpc_timeout_seconds,
        )

    def get_object_metadata(self, object_id: int, object_token: str):
        return self.object_stub.GetObjectMetadata(
            openevent_pb2.GetObjectMetadataRequest(
                object_id=object_id,
                object_token=object_token,
            ),
            timeout=self._rpc_timeout_seconds,
        )

    def read_object(self, object_id: int, object_token: str, offset: int, nbytes: int):
        return self.object_stub.ReadObject(
            openevent_pb2.ReadObjectRequest(
                object_id=object_id,
                object_token=object_token,
                offset=offset,
                nbytes=nbytes,
            ),
            timeout=self._rpc_timeout_seconds,
        )

    def create_channel(
        self,
        principal: int,
        token: str,
        name: str,
        visibility: int = openevent_pb2.VISIBILITY_PUBLIC,
        protocol: str = "",
        description: str = "",
        members: Iterable[int] = (),
    ):
        return self.channel_stub.CreateChannel(
            openevent_pb2.CreateChannelRequest(
                principal=principal,
                token=token,
                name=name,
                visibility=visibility,
                protocol=protocol,
                description=description,
                members=list(members),
            ),
            timeout=self._rpc_timeout_seconds,
        )

    def get_channel(self, principal: int, token: str, channel_id: int):
        return self.channel_stub.GetChannel(
            openevent_pb2.GetChannelRequest(principal=principal, token=token, channel_id=channel_id),
            timeout=self._rpc_timeout_seconds,
        )

    def list_channels(
        self,
        principal: int,
        token: str,
        filter: int = openevent_pb2.CHANNEL_FILTER_ALL,
    ):
        return self.channel_stub.ListChannels(
            openevent_pb2.ListChannelsRequest(principal=principal, token=token, filter=filter),
            timeout=self._rpc_timeout_seconds,
        )

    def add_member(self, principal: int, token: str, channel_id: int, target_principal: int):
        return self.channel_stub.AddMember(
            openevent_pb2.AddMemberRequest(
                principal=principal,
                token=token,
                channel_id=channel_id,
                target_principal=target_principal,
            ),
            timeout=self._rpc_timeout_seconds,
        )

    def remove_member(self, principal: int, token: str, channel_id: int, target_principal: int):
        return self.channel_stub.RemoveMember(
            openevent_pb2.RemoveMemberRequest(
                principal=principal,
                token=token,
                channel_id=channel_id,
                target_principal=target_principal,
            ),
            timeout=self._rpc_timeout_seconds,
        )
