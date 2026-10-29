"""Errors which are safe to expose as tool data (never include request URLs)."""


class DataError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class NeedsInput(DataError):
    pass

