"""Linux peer credentials.

``PeerIdentity`` can only be built from ``SO_PEERCRED``. A caller-supplied
owner flag is not a credential.
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass
from typing import Literal

from sterling.gate2.errors import ProtocolError

# Linux SOL_SOCKET / SO_PEERCRED.
_SO_PEERCRED = 17
_UCRED = struct.Struct("3I")


@dataclass(frozen=True, slots=True)
class PeerIdentity:
    """OS identity of a connected local peer."""

    pid: int
    uid: int
    gid: int
    source: Literal["SO_PEERCRED"]


def peer_from_socket(connection: socket.socket) -> PeerIdentity:
    """Read the kernel identity of ``connection``. Forged JSON cannot supply this."""
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, _SO_PEERCRED, _UCRED.size)
    except OSError as exc:
        raise ProtocolError("peer credentials are unavailable") from exc
    pid, uid, gid = _UCRED.unpack(raw)
    return PeerIdentity(pid=int(pid), uid=int(uid), gid=int(gid), source="SO_PEERCRED")
