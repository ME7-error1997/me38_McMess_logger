"""McMESS logger for Bosch M3.8 ECUs (06A906018CG / 1.8T AGU).

Reads ECU RAM over the K-line using the 018CG's read-only McMESS responder, from a
plain FTDI serial cable -- 9-bit frames are produced by pinning the UART parity bit.

See docs/mcmess-018cg.md for the protocol and what is still unverified.
"""

from .errors import (
    DesyncError,
    HandshakeError,
    McMessError,
    NotConnectedError,
    PortError,
    TimeoutError_,
)
from .session import (
    ACK_BYTE,
    REQ_FAST,
    REQ_SLOW,
    ConnectResult,
    McMessSession,
    ReadProgram,
    build_program,
)
from .transport import BAUD_FAST, BAUD_SLOW, KLineTransport, TransportStats

__all__ = [
    "ACK_BYTE",
    "BAUD_FAST",
    "BAUD_SLOW",
    "ConnectResult",
    "DesyncError",
    "HandshakeError",
    "KLineTransport",
    "McMessError",
    "McMessSession",
    "NotConnectedError",
    "PortError",
    "REQ_FAST",
    "REQ_SLOW",
    "ReadProgram",
    "TimeoutError_",
    "TransportStats",
    "build_program",
]
