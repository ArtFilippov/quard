---
name: implement-spec
description: "Implement the result of /to-spec and /to-tickets in code."
disable-model-invocation: true
---

You have been provided a spec. This spec should have tickets associated with it, describing how to implement the spec.

The issue tracker should have been provided to you. If not, tell the user to run `/setup-matt-pocock-skills`.

The goal is the entire spec implemented, with every ticket resolved the way the issue tracker closes work.

The tickets are not a list of steps. They are a **task graph** with blocking relationships between them. This means there is always a **frontier** of tickets which are ready to be grabbed.

Communicate primarily through **context pointers**: to the spec, tickets, and research notes. Don't duplicate information already available via pointers.

## Steps

1. Read the spec and tickets to understand the task graph.

2. Explore relevant codebase files or external documentation as required by the tickets. Save reusable notes where later work can find them.

3. Work through the ready **frontier** in dependency order. Use `tdd` to build each ticket and run its relevant checks.

4. Resolve completed tickets in the tracker and revisit the frontier until all tickets are complete.

5. Call the Skill tool with `code-review` on the completed work. Fix issues raised by the review and run the full test suite.

6. Report the completed work and verification.
