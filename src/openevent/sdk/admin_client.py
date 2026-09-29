from __future__ import annotations

import grpc

from .client import _DEFAULT_CHANNEL_OPTIONS, _DEFAULT_RPC_TIMEOUT_MS, _validate_timeout_ms

from .proto import admin_pb2, admin_pb2_grpc


class AdminClient:
    def __init__(
        self,
        target: str,
        timeout_ms: float = _DEFAULT_RPC_TIMEOUT_MS,
    ):
        self._closed = True
        self.timeout_ms = timeout_ms
        self.channel = grpc.insecure_channel(target, options=_DEFAULT_CHANNEL_OPTIONS)
        self._closed = False
        try:
            self.stub = admin_pb2_grpc.AdminServiceStub(self.channel)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        if hasattr(self, "channel"):
            self.channel.close()

    def __enter__(self) -> AdminClient:
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

    def add_token(self, principal: int):
        return self.stub.AddToken(
            admin_pb2.AddTokenRequest(principal=principal),
            timeout=self._rpc_timeout_seconds,
        )

    def delete_token(self, principal: int, token: str):
        return self.stub.DeleteToken(
            admin_pb2.DeleteTokenRequest(principal=principal, token=token),
            timeout=self._rpc_timeout_seconds,
        )

    def list_messages(self, from_seq: int = 0, limit: int = 1000):
        return self.stub.ListMessages(
            admin_pb2.ListMessagesRequest(
                from_seq=from_seq,
                limit=limit,
            ),
            timeout=self._rpc_timeout_seconds,
        )
