"""Shared runtime error contract, independent of utility implementation."""


class QuardError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
