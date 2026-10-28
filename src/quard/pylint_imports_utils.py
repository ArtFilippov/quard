"""Reuse Pylint's isort configuration so native import classification can cache."""
from __future__ import annotations

from importlib import metadata

import isort
from pylint.checkers.imports import ImportsChecker

from quard.pylint_similarity_utils import SUPPORTED_VERSION

CACHE_KEY = "_quard_isort_config"


def install() -> None:
    """Retain one native isort configuration per Pylint import checker.

    Raises:
        RuntimeError: The pinned Pylint import checker has changed.
    """
    if metadata.version("pylint") != SUPPORTED_VERSION:
        raise RuntimeError("Pylint import caching requires review for this version")
    original = ImportsChecker._isort_config

    def configured(checker: ImportsChecker) -> isort.Config:
        if CACHE_KEY not in checker.__dict__:
            checker.__dict__[CACHE_KEY] = original.__get__(checker)
        return checker.__dict__[CACHE_KEY]

    ImportsChecker._isort_config = property(configured)
