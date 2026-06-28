from __future__ import annotations

from typing import Iterable, Optional
import grpc

try:
    from .proto import openevent_pb2, openevent_pb2_grpc
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "generated protobuf modules are missing; run make build from openevent-sdk"
    ) from exc


class OpenEventClient:
    def __init__(self, target: str, channel: Optional[grpc.Channel] = None):
        self.channel = channel or grpc.insecure_channel(target)
        self.event_stub = openevent_pb2_grpc.EventServiceStub(self.channel)
        self.channel_stub = openevent_pb2_grpc.ChannelServiceStub(self.channel)

    def get_status(self, principal: int, token: str):
        return self.event_stub.GetStatus(openevent_pb2.GetStatusRequest(principal=principal, token=token))

    def publish(
        self,
        principal: int,
        token: str,
        channel_id: int,
        seq: int,
        payload: bytes,
        recipients: Iterable[int] = (),
    ):
        return self.event_stub.Publish(
            openevent_pb2.PublishRequest(
                principal=principal,
                token=token,
                channel_id=channel_id,
                seq=seq,
                recipients=list(recipients),
                payload=payload,
            )
        )

    def publish_auto_seq(
        self,
        principal: int,
        token: str,
        channel_id: int,
        payload: bytes,
        recipients: Iterable[int] = (),
    ):
        return self.event_stub.PublishAutoSeq(
            openevent_pb2.PublishAutoSeqRequest(
                principal=principal,
                token=token,
                channel_id=channel_id,
                recipients=list(recipients),
                payload=payload,
            )
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
            )
        )

    def subscribe(
        self,
        principal: int,
        token: str,
        from_seq: int = 0,
        only_my_recipient: bool = False,
    ):
        return self.event_stub.Subscribe(
            openevent_pb2.SubscribeRequest(
                principal=principal,
                token=token,
                from_seq=from_seq,
                only_my_recipient=only_my_recipient,
            )
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
            )
        )

    def get_channel(self, principal: int, token: str, channel_id: int):
        return self.channel_stub.GetChannel(
            openevent_pb2.GetChannelRequest(principal=principal, token=token, channel_id=channel_id)
        )

    def list_channels(
        self,
        principal: int,
        token: str,
        filter: int = openevent_pb2.CHANNEL_FILTER_ALL,
    ):
        return self.channel_stub.ListChannels(
            openevent_pb2.ListChannelsRequest(principal=principal, token=token, filter=filter)
        )

    def add_member(self, principal: int, token: str, channel_id: int, target_principal: int):
        return self.channel_stub.AddMember(
            openevent_pb2.AddMemberRequest(
                principal=principal,
                token=token,
                channel_id=channel_id,
                target_principal=target_principal,
            )
        )

    def remove_member(self, principal: int, token: str, channel_id: int, target_principal: int):
        return self.channel_stub.RemoveMember(
            openevent_pb2.RemoveMemberRequest(
                principal=principal,
                token=token,
                channel_id=channel_id,
                target_principal=target_principal,
            )
        )


class AdminClient:
    def __init__(self, target: str, channel: Optional[grpc.Channel] = None):
        self.channel = channel or grpc.insecure_channel(target)
        self.stub = openevent_pb2_grpc.AdminServiceStub(self.channel)

    def add_token(self, target_principal: int):
        return self.stub.AddToken(openevent_pb2.AddTokenRequest(target_principal=target_principal))

    def delete_token(self, target_token: str):
        return self.stub.DeleteToken(openevent_pb2.DeleteTokenRequest(target_token=target_token))

    def list_tokens(self):
        return self.stub.ListTokens(openevent_pb2.ListTokensRequest())
