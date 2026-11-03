# Privacy

## What leaves your machine

Exactly two things, and only when the decision is `SEND`:

1. **The hook** — one paragraph of text extracted from your own files — is passed
   to whatever `generator.command` you configured. If that is a hosted model,
   the hook leaves your machine.
2. **The generated line** is passed to `sender.command`.

Nothing is sent on `DEFER` or `SILENT`. Nothing is sent when `--dry-run` or
`--test` is used.

## What is logged

`decisions.jsonl` records: timestamp, decision, reasons, a truncated candidate
summary, scores, and the mode. It does **not** store the full generated message
unless you enable it.

Keep `state/` out of version control. The shipped `.gitignore` does this.

## Hooks are derived from private content

The four inputs (facts, lessons, diary, conversations) are the most private
files you own. A hook is a *selection* from them, not a paraphrase — which means
a hook can contain a real name, a real date, a real project.

Mitigations available today:

- Point `[inputs]` at a curated subset rather than your whole diary.
- Set `generator.command` to a **local** model. The gate itself makes no network
  calls.
- Run with `--test` first and read the hook line it picks.

## Content is data, not instructions

Diary text and chat JSONL are parsed as data. The gate never executes file
content. However, the hook is embedded into the generator prompt — if your
generator is an LLM, treat any file that can contain adversarial text as
untrusted input to *that* model.

## Purging

```bash
rm -rf state/          # decisions, quota history, outbox
```

Removing `state/` resets quota counters. It does not touch your memory files.
