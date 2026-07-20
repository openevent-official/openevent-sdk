from .admin_client import AdminClient
from .client import OpenEventClient

try:
    from .proto import admin_pb2, admin_pb2_grpc, openevent_pb2, openevent_pb2_grpc
except ImportError:  # pragma: no cover
    admin_pb2 = None
    admin_pb2_grpc = None
    openevent_pb2 = None
    openevent_pb2_grpc = None

__all__ = [
    "AdminClient",
    "OpenEventClient",
    "admin_pb2",
    "admin_pb2_grpc",
    "openevent_pb2",
    "openevent_pb2_grpc",
]
