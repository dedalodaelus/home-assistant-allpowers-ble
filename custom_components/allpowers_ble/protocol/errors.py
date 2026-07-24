"""Protocol-specific exceptions."""


class ProtocolError(ValueError):
    """Base exception for malformed or unsupported protocol data."""


class FrameTooShortError(ProtocolError):
    """Raised when a frame cannot contain the required envelope."""


class InvalidHeaderError(ProtocolError):
    """Raised when the protocol header is invalid."""


class InvalidLengthError(ProtocolError):
    """Raised when the encoded and actual frame lengths differ."""


class InvalidChecksumError(ProtocolError):
    """Raised when the XOR checksum is invalid."""


class InvalidPayloadError(ProtocolError):
    """Raised when a command payload is malformed."""


class StateUnavailableError(RuntimeError):
    """Raised when a safe write cannot be built from a fresh state snapshot."""
