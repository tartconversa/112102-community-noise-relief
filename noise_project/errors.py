"""Error types shared by the local application."""


class SourceError(ValueError):
    """A source table is malformed or internally inconsistent."""


class StoreError(RuntimeError):
    """The local decision/commitment store cannot satisfy a request."""


class WorkflowError(RuntimeError):
    """A requested workflow transition is not allowed by the evidence or the rules."""
