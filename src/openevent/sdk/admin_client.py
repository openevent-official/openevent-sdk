from __future__ import annotations

from typing import Optional

import grpc

from .client import _DEFAULT_CHANNEL_OPTIONS, _DEFAULT_RPC_TIMEOUT_SECONDS, _validate_rpc_timeout

try:
    from .proto import admin_pb2, admin_pb2_grpc
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "generated protobuf modules are missing; run make build from openevent-sdk"
    ) from exc


class AdminClient:
    def __init__(
        self,
        target: str,
        channel: Optional[grpc.Channel] = None,
        timeout: float = _DEFAULT_RPC_TIMEOUT_SECONDS,
    ):
        self._closed = True
        self._owns_channel = channel is None
        self.timeout = timeout
        self.channel = (
            grpc.insecure_channel(target, options=_DEFAULT_CHANNEL_OPTIONS)
            if channel is None
            else channel
        )
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
        if getattr(self, "_owns_channel", False):
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
    def timeout(self) -> float:
        return self._timeout

    @timeout.setter
    def timeout(self, value: object) -> None:
        self._timeout = _validate_rpc_timeout(value)

    def add_token(self, target_principal: int):
        return self.stub.AddToken(
            admin_pb2.AddTokenRequest(target_principal=target_principal),
            timeout=self.timeout,
        )

    def delete_token(self, target_token: str):
        return self.stub.DeleteToken(
            admin_pb2.DeleteTokenRequest(target_token=target_token),
            timeout=self.timeout,
        )

    def list_tokens(self, page_token: str = "", limit: int = 1000):
        return self.stub.ListTokens(
            admin_pb2.ListTokensRequest(
                page_token=page_token,
                limit=limit,
            ),
            timeout=self.timeout,
        )

    def list_messages(self, from_seq: int = 0, limit: int = 1000):
        return self.stub.ListMessages(
            admin_pb2.ListMessagesRequest(
                from_seq=from_seq,
                limit=limit,
            ),
            timeout=self.timeout,
        )
