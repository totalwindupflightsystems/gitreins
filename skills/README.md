# GitReins skills — knowledge for agents and humans

This directory holds **agent-readable skill documents** about GitReins: how to
install it, configure it, tune it, operate it, and debug it. They are written
for an AI agent that has been dropped into a repo that uses GitReins (or is
working *on* GitReins itself) and needs to get it right the first time instead
of rediscovering the same traps.

## Layout

Each skill is a directory with a single `SKILL.md`:

```
skills/
  <skill-name>/SKILL.md
```

`SKILL.md` starts with YAML frontmatter, then prose. That is the whole
convention — no build step, no registry. If your agent harness supports
loadable skills, point it at this directory; if it does not, `SKILL.md` is
plain markdown that any agent can be handed as context.

## Frontmatter

```yaml
---
name: <skill-name>            # matches the directory name
description: >-               # one screen, states WHEN to load it
  <what it covers and when an agent should read it>
version: <x.y.z>              # bump when the content changes
category: software-development
---
```

## How an agent should use these

1. **Load by trigger, not by bulk.** Read only the skill whose `description`
   matches the task in front of you. Loading all of them wastes context.
2. **Treat the repo as the authority.** Every skill states the release and
   commit it was verified against. When a skill and the code disagree, the code
   wins — and the skill is a bug worth fixing.
3. **Prefer the narrow skill.** `gitreins-settings` for knobs,
   `gitreins-judge-tuning` for evaluator cost/latency/quality,
   `gitreins-troubleshooting` when something failed,
   `gitreins-agent-operations` for the task/MCP/commit workflow,
   `gitreins-known-failures` for the recurring traps,
   `gitreins-release-and-versions` for installed-vs-released drift.

## Skills

| Skill | Load it when |
|---|---|
| `gitreins-usage` | You need the end-to-end workflow: install, task lifecycle, guards, judge, MCP, report |
| `gitreins-settings` | You are configuring GitReins — every knob, env var, precedence and safe range |
| `gitreins-judge-tuning` | The evaluator is too slow, too expensive, or wrong — context, criteria, caps, models |
| `gitreins-troubleshooting` | A guard, judge or commit failed and you need the diagnostic ladder |
| `gitreins-agent-operations` | You are an agent driving GitReins: tasks, MCP tools, worker briefs, commits |
| `gitreins-known-failures` | Something smells like a familiar trap; check the recurring failure catalogue first |
| `gitreins-release-and-versions` | The installed binary, PyPI, and the repo disagree about what version you are running |

## Contributing a skill

- One topic per skill; if a file grows past ~1,500 lines, split it.
- Lead with the trigger, then the commands, then the failure modes.
- Cite the source of a claim as `path/to/file.py:123` so a reader can re-verify.
- Never include credentials, tokens or private host paths — use `[REDACTED]`
  and `~/` style paths.
- State the version/commit you verified against; delete claims you cannot
  re-verify rather than leaving them to rot.
