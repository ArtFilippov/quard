"""Offline regressions for the AFK issue runner; no agent or GitHub calls."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, cast
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "ralph.sh"

GIT = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
s = json.loads(Path(os.environ["MOCK_STATE"]).read_text())
a = sys.argv[1:]
if a == ["rev-parse", "--is-inside-work-tree"]: print("true")
elif a == ["symbolic-ref", "--quiet", "--short", "HEAD"]: print("main")
elif a == ["rev-parse", "--verify", "@{upstream}"]: print(s["upstream"])
elif a == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"]: print("origin/main")
elif a == ["rev-parse", "HEAD"]: print(s["head"])
elif a == ["rev-parse", "@{upstream}"]: print(s["upstream"])
elif a == ["status", "--porcelain", "--untracked-files=normal"]:
    if s.get("dirty"): print("?? user-change")
else: raise SystemExit(f"unexpected git: {a}")
'''

GH = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
s = json.loads(Path(os.environ["MOCK_STATE"]).read_text())
a = sys.argv[1:]
if a[:2] == ["api", "--method"]:
    issues = [issue for issue in s["issues"] if issue["state"] == "open" and
              "ready-for-agent" in issue["labels"]]
    print(json.dumps([issues]))
elif a[0] == "api" and "/issues/" in a[1]:
    n = int(a[1].rsplit("/", 1)[-1])
    issue = next(issue for issue in s["issues"] if issue["number"] == n)
    print(issue["state"])
else: raise SystemExit(f"unexpected gh: {a}")
'''

OPENCODE = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
path = Path(os.environ["MOCK_STATE"])
s = json.loads(path.read_text())
a = sys.argv[1:]
if a[:2] == ["export", "--sanitize"]:
    assert a[2] == "ses_2"
    s.setdefault("proxy_seen", []).append({"command": "export", "http": os.environ.get("HTTP_PROXY"),
        "https": os.environ.get("HTTPS_PROXY"), "no": os.environ.get("NO_PROXY"),
        "lower_no": os.environ.get("no_proxy")})
    path.write_text(json.dumps(s))
    print(json.dumps({"messages": [{"info": {"role": "assistant", "providerID": "provider",
        "modelID": "actual-model", "variant": "high"}}]}))
    raise SystemExit(0)
assert a[:3] == ["run", "--format", "json"] and "--auto" in a
prompt = a[-1]
session = "ses_2"
if "--session" in a: assert a[a.index("--session") + 1] == session
else: assert prompt.startswith("Implement issue #2.")
s.setdefault("proxy_seen", []).append({"command": "run", "http": os.environ.get("HTTP_PROXY"),
    "https": os.environ.get("HTTPS_PROXY"), "no": os.environ.get("NO_PROXY"),
    "lower_no": os.environ.get("no_proxy")})
s.setdefault("model_flags", []).append({"model": a[a.index("--model") + 1] if "--model" in a else None,
    "variant": a[a.index("--variant") + 1] if "--variant" in a else None})
s["prompts"].append(prompt)
if prompt == "Close the issue, commit all changes and push":
    next(issue for issue in s["issues"] if issue["number"] == 2)["state"] = "closed"
    if s.get("push_fails"):
        path.write_text(json.dumps(s))
        raise SystemExit(1)
    s["head"] = s["upstream"] = "new-commit"
    text = "Closed, committed and pushed"
elif prompt.startswith("Return only the XML"):
    text = "not XML" if s.get("invalid_correction") else '<result status="success"><summary>Verified</summary></result>'
elif s.get("bad_xml"):
    text = "not XML"
elif s.get("failure_xml"):
    text = '<result status="failure"><summary>Checks failed</summary></result>'
else:
    text = '<result status="success"><summary>Verified</summary></result>'
path.write_text(json.dumps(s))
for event in [
    {"type": "step_start", "part": {}},
    {"type": "tool_use", "part": {"tool": "bash", "state": {"status": "completed", "input": {"command": "check"}, "output": "passed"}}},
    {"type": "step_finish", "part": {"tokens": {"input": 100, "output": 20, "cache": {"read": 30, "write": 10}}}},
    {"type": "text", "part": {"text": text}},
]:
    print(json.dumps({"sessionID": session, **event}), flush=True)
'''


def issue(number: int, *, state: str = "open", assignees: list[dict[str, str]] | None = None,
          body: str = "", blocked: int = 0) -> dict[str, object]:
    return {"number": number, "title": f"Issue {number}", "state": state,
            "labels": ["ready-for-agent"], "assignees": assignees or [],
            "body": body, "issue_dependencies_summary": {"blocked_by": blocked}}


class RalphTests(unittest.TestCase):
    def run_runner(
        self, args: list[str] | None = None, **overrides: bool
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        with tempfile.TemporaryDirectory(prefix="eryx ralph ") as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            for name, code in (("git", GIT), ("gh", GH), ("opencode", OPENCODE)):
                program = bin_dir / name
                program.write_text(code)
                program.chmod(0o755)
            state_file = root / "state.json"
            state: dict[str, Any] = {
                "head": "old-commit", "upstream": "old-commit", "prompts": [],
                "issues": [issue(5, blocked=1), issue(4, assignees=[{"login": "someone"}]),
                           issue(2, body="## Blocked by\n- #1\n"), issue(1, state="closed")],
                **overrides,
            }
            state_file.write_text(json.dumps(state))
            env = os.environ | {"MOCK_STATE": str(state_file),
                                "PATH": f"{bin_dir}:{os.environ['PATH']}"}
            if args is None:
                args = ["HTTP_PROXY=http://proxy.example:8080",
                        "HTTPS_PROXY=http://secure.example:8443"]
            result = subprocess.run(["bash", str(SCRIPT), *args], cwd=root, env=env,
                                    text=True, capture_output=True, timeout=30, check=False)
            return result, cast(dict[str, Any], json.loads(state_file.read_text()))

    def test_deterministic_issue_selection_session_and_completion(self):
        result, state = self.run_runner()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(state["prompts"]), 2)
        self.assertIn("Implement issue #2", state["prompts"][0])
        self.assertEqual(state["prompts"][1], "Close the issue, commit all changes and push")
        self.assertIn("Пропуск #4: назначен исполнитель", result.stdout)
        self.assertIn("контекст ≈140 токенов", result.stdout)
        self.assertIn("model provider/actual-model | variant high", result.stdout)
        self.assertIn("доступных issues больше нет", result.stdout)

    def test_proxy_and_no_proxy_are_applied_to_every_opencode_command(self):
        args = ["HTTPS_PROXY=http://user:secret@https.example:8080",
                "HTTP_PROXY=http://user:secret@http.example:8080",
                "--model", "provider/actual-model", "--variant", "high"]
        result, state = self.run_runner(args=args, bad_xml=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([entry["command"] for entry in state["proxy_seen"]],
                         ["run", "export", "run", "export", "run", "export"])
        for entry in state["proxy_seen"]:
            self.assertEqual(entry["http"], "http://user:secret@http.example:8080")
            self.assertEqual(entry["https"], "http://user:secret@https.example:8080")
            self.assertEqual(entry["no"], "localhost,127.0.0.1,::1")
            self.assertEqual(entry["lower_no"], entry["no"])
        self.assertEqual(state["model_flags"],
                         [{"model": "provider/actual-model", "variant": "high"}] * 3)
        self.assertNotIn("secret", result.stdout + result.stderr)

    def test_missing_proxy_arguments_stop_before_agent(self):
        result, state = self.run_runner(args=["HTTP_PROXY=http://proxy.example"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["prompts"], [])
        self.assertIn("HTTPS_PROXY=URL", result.stderr)

    def test_invalid_xml_is_repaired_in_same_session(self):
        result, state = self.run_runner(bad_xml=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(state["prompts"]), 3)
        self.assertIn("Уточняю XML", result.stdout)

    def test_invalid_xml_stops_after_bounded_retries(self):
        result, state = self.run_runner(bad_xml=True, invalid_correction=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(state["prompts"]), 3)
        self.assertIn("после 3 попыток", result.stderr)

    def test_reported_failure_does_not_close_or_push(self):
        result, state = self.run_runner(failure_xml=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(state["prompts"]), 1)
        self.assertIn("Checks failed", result.stderr)

    def test_push_failure_stops_even_if_issue_was_closed(self):
        result, state = self.run_runner(push_fails=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(state["prompts"]), 2)
        self.assertIn("ses_2", result.stderr)
        self.assertEqual(state["head"], "old-commit")

    def test_dirty_checkout_is_rejected_before_agent_runs(self):
        result, state = self.run_runner(dirty=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["prompts"], [])
        self.assertIn("рабочая копия не чистая", result.stderr)


if __name__ == "__main__":
    unittest.main()
