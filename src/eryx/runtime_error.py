"""Shared runtime error contract, independent of utility implementation."""


class EryxError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
