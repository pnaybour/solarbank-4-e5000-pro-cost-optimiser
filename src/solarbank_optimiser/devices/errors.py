class DeviceError(Exception):
    """A device could not be read. `kind` says why; the value is then unknown, never stale."""

    KINDS = ("connect", "timeout", "closed", "protocol", "exception", "invalid", "identity")

    def __init__(self, kind, message):
        if kind not in self.KINDS:
            raise ValueError("Unknown device error kind")
        super().__init__(message)
        self.kind = kind
