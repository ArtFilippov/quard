"""Find identifying path components and remote logins without retaining their values."""
from __future__ import annotations

import re

from presidio_analyzer import EntityRecognizer, RecognizerResult


# Only the account component of these conventional per-user directories is a
# finding. Other absolute paths (including /tmp and /usr) are not evidence of
# an identity. Exclude conventional shared directory names.
LOCAL_PATH = re.compile(
    r"(?<![\w/])/(?:home|Users|scratch|mnt/scratch)/"
    r"(?P<account>[\w][\w.-]*)(?=$|[/\\\s'\"`),;:\]}])", re.UNICODE)
SHARED_DIRECTORIES = frozenset({"shared", "public", "common", "project", "projects", "data", "tmp"})

# A command boundary and a remote operand are both required. The account alone
# is reported; the host, remote path and surrounding command are never findings.
COMMAND = re.compile(r"(?<![\w./-])(?P<command>ssh|scp|sftp)(?![\w-])(?P<args>[^\n;&|#]*)", re.IGNORECASE)
REMOTE = re.compile(
    r"(?<![\w@])(?P<account>[a-z_][a-z0-9_.-]*)@"
    r"(?:[a-z0-9][a-z0-9.-]*|\[[0-9a-f:.]+\])"
    r"(?=$|[:\s'\"`),;|/])", re.IGNORECASE)


class LocalPathRecognizer(EntityRecognizer):
    """Recognize a user directory component, not the whole path."""

    def __init__(self, language: str):
        super().__init__(["LOCAL_PATH_IDENTIFIER"], name="LocalPathRecognizer", supported_language=language)

    def analyze(self, text, entities, nlp_artifacts):
        if "LOCAL_PATH_IDENTIFIER" not in entities:
            return []
        return [RecognizerResult("LOCAL_PATH_IDENTIFIER", *match.span("account"), 0.7)
                for match in LOCAL_PATH.finditer(text)
                if match.group("account").casefold() not in SHARED_DIRECTORIES]


class RemoteAccountRecognizer(EntityRecognizer):
    """Recognize user@host operands of ssh, scp and sftp commands."""

    def __init__(self, language: str):
        super().__init__(["REMOTE_ACCOUNT"], name="RemoteAccountRecognizer", supported_language=language)

    def analyze(self, text, entities, nlp_artifacts):
        if "REMOTE_ACCOUNT" not in entities:
            return []
        results = []
        for command in COMMAND.finditer(text):
            args = command.group("args")
            for operand in REMOTE.finditer(args):
                if command.group("command").lower() == "scp" and args[operand.end():operand.end() + 1] != ":":
                    continue
                start = command.start("args") + operand.start("account")
                results.append(RecognizerResult("REMOTE_ACCOUNT", start,
                                                start + len(operand.group("account")), 0.7))
        return results


def add_contextual_recognizers(engine) -> None:
    """Register the same context rules for both independent language passes."""
    for language in ("en", "ru"):
        engine.registry.add_recognizer(LocalPathRecognizer(language))
        engine.registry.add_recognizer(RemoteAccountRecognizer(language))
