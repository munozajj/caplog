"""Errors the HTTP layer turns into status codes."""


class CaplogError(Exception):
    pass


class ValidationError(CaplogError):
    """The request was rejected. Maps to HTTP 400."""


class ErrNoTitle(CaplogError):
    def __init__(self) -> None:
        super().__init__("the logbook has not been set up yet (no title)")


class ErrTitleIsSet(CaplogError):
    def __init__(self) -> None:
        super().__init__("the logbook title is already set and can never be changed")


class ErrDenied(CaplogError):
    def __init__(self, message: str = "not allowed") -> None:
        super().__init__(message)


class ErrLocked(CaplogError):
    def __init__(self) -> None:
        super().__init__("this account is locked after repeated failed sign-in attempts")


class ErrRateLimited(CaplogError):
    def __init__(self) -> None:
        super().__init__("too many posts, try again later")
