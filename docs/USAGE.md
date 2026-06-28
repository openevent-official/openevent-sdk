# OpenEvent SDK Usage Guide

[中文版](USAGE_cn.md)

This document explains how community users can install and use `openevent-sdk`
in Python projects. Protocol fields and boundary semantics are defined by
[the API contract](API.md).

## Install

Build a wheel from this repository:

```bash
make build
python3 -m pip install dist/openevent_sdk-0.3.0-py3-none-any.whl
```

For local development, generate protobuf code and use the source tree directly:

```bash
make init
export PYTHONPATH="$PWD/src"
```

## Connect to the Service

```python
from openevent.sdk import OpenEventClient

client = OpenEventClient("127.0.0.1:9527")

principal = 1001
token = "user-token"
```

`target` is the gRPC service address. By default the SDK uses
`grpc.insecure_channel`. If your deployment requires TLS or a custom channel,
create a `grpc.Channel` and pass it with `OpenEventClient(target, channel=...)`.

## Create a Channel

```python
from openevent.sdk.proto import openevent_pb2

created = client.create_channel(
    principal=principal,
    token=token,
    name="general",
    visibility=openevent_pb2.VISIBILITY_PUBLIC,
    protocol="text/plain",
    description="Community discussion",
)

channel_id = created.channel.channel_id
```

Visibility:

- `VISIBILITY_PUBLIC`: all authenticated users can read and write.
- `VISIBILITY_PROTECTED`: all authenticated users can read; members can write.
- `VISIBILITY_PRIVATE`: members can read and write.

## Publish Messages

The simplest method is to let the server assign a global `seq`:

```python
payload = "hello OpenEvent".encode("utf-8")

result = client.publish_auto_seq(
    principal=principal,
    token=token,
    channel_id=channel_id,
    payload=payload,
)

print(result.seq)
```

If the client needs CAS-style publishing, query status first and then call
`publish`:

```python
status = client.get_status(principal, token)
next_seq = status.max_seq + 1

client.publish(
    principal=principal,
    token=token,
    channel_id=channel_id,
    seq=next_seq,
    payload=b"message with expected seq",
)
```

`payload` is an opaque byte array. You can encode it as JSON, MessagePack,
custom binary, or plain text.

## Fetch Messages

```python
response = client.fetch(
    principal=principal,
    token=token,
    from_seq=1,
    limit=100,
    channels=[channel_id],
)

for message in response.messages:
    print(message.seq, message.channel_id, message.payload)

next_seq = response.next_seq
last_seq = response.last_seq
```

`response.messages` contains `EventMessage` values. Pass `channels=[...]` to
fetch only selected channels; omit it or pass an empty list to fetch all channels
visible to the caller. Use the previous response's `next_seq` as the next
`from_seq` to continue reading. If `next_seq <= last_seq`, another fetch may scan
more committed messages; if `next_seq > last_seq`, the response has reached the
current committed tail.

To fetch only messages targeted to the current principal:

```python
response = client.fetch(
    principal=principal,
    token=token,
    from_seq=1,
    limit=100,
    channels=[channel_id],
    only_my_recipient=True,
)
```

## Subscribe to Messages

`subscribe` returns a gRPC stream:

```python
for item in client.subscribe(principal, token, from_seq=0):
    if item.HasField("message"):
        message = item.message
        print(message.seq, message.payload)
    else:
        print("next seq:", item.next_seq)
```

`from_seq=0` means wait for messages published after the subscription is
established. If the connection is interrupted, the client should record the last
processed `seq` and resume with the next expected `seq`.

## Targeted Messages

Pass `recipients` when publishing:

```python
client.publish_auto_seq(
    principal=principal,
    token=token,
    channel_id=channel_id,
    recipients=[1002, 1003],
    payload=b"private note for selected members",
)
```

Each recipient principal must be a member of the channel. When reading with
`only_my_recipient=True`, the server returns only messages whose `recipients`
include the current `principal`.

## Manage Tokens

`AdminClient` accesses `AdminService`. This service usually listens on a
separate admin port; whether it is exposed externally depends on deployment
configuration.

```python
from openevent.sdk import AdminClient

admin = AdminClient("127.0.0.1:9528")

binding = admin.add_token(target_principal=1001).binding
print(binding.principal, binding.token)

for item in admin.list_tokens().bindings:
    print(item.principal, item.token)

admin.delete_token(binding.token)
```

## Error Handling

The SDK exposes gRPC call errors directly. Callers should catch `grpc.RpcError`
and branch on the status code:

```python
import grpc

try:
    client.publish_auto_seq(principal, token, channel_id, b"hello")
except grpc.RpcError as exc:
    if exc.code() == grpc.StatusCode.UNAUTHENTICATED:
        print("invalid token")
    elif exc.code() == grpc.StatusCode.PERMISSION_DENIED:
        print("no permission")
    else:
        raise
```

See [the API contract](API.md) for common status codes and complete semantics.

## Local Development

Common commands:

```bash
make init
make test
make build
make clean
```

`make init` generates Python protobuf code under
[`src/openevent/sdk/proto/`](../src/openevent/sdk/proto/).
Generated `openevent_pb2*.py` files are not tracked by Git, but are included in
the wheel.

`make build` uses Hatchling to build a wheel. Build dependencies, test
dependencies, caches, and temporary files are placed under `build/`, and final
artifacts are placed under `dist/`.
