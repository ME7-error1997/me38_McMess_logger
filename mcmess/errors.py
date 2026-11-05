"""Exception types for the McMESS logger."""


class McMessError(Exception):
    """Base class for all McMESS failures."""


class PortError(McMessError):
    """The serial port could not be opened or configured."""


class TimeoutError_(McMessError):
    """The ECU did not answer within the allowed window."""


class HandshakeError(McMessError):
    """Session start failed (no ack, or an unexpected ack byte)."""


class DesyncError(McMessError):
    """The byte stream no longer lines up with the lock-step request pattern."""


class NotConnectedError(McMessError):
    """An operation needed a live session and there is none."""
