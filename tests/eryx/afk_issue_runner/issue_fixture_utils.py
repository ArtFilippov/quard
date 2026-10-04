"""Issue payloads for the offline shell-runner fixtures."""


def issue(number: int, *, state: str = "open", assignees: list[dict[str, str]] | None = None,
          body: str = "", blocked: int = 0) -> dict[str, object]:
    return {"number": number, "title": f"Issue {number}", "state": state,
            "labels": ["ready-for-agent"], "assignees": assignees or [],
            "body": body, "issue_dependencies_summary": {"blocked_by": blocked}}
