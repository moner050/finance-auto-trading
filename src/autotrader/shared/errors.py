class DomainInvariantError(ValueError):
    """Raised when a pure domain invariant is violated."""


class FloatRejectedError(TypeError):
    """Raised when a binary floating-point value crosses a domain boundary."""
