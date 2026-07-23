from __future__ import annotations

from typing import Optional

import grpc

from .client import _DEFAULT_CHANNEL_OPTIONS, _DEFAULT_RPC_TIMEOUT_SECONDS

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
        timeout: Optional[float] = _DEFAULT_RPC_TIMEOUT_SECONDS,
    ):
        self.channel = channel or grpc.insecure_channel(target, options=_DEFAULT_CHANNEL_OPTIONS)
        self.timeout = timeout
        self.stub = admin_pb2_grpc.AdminServiceStub(self.channel)

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
