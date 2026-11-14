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
