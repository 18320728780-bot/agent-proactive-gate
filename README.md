# agent-proactive-gate

A policy gate for proactive agent messages: **send, defer, or stay silent.**

Most "proactive agent" implementations fire a scheduled prompt — and out comes
"are you busy?" three times a day. This is the opposite: the timer only *checks
candidates*. Whether anything gets said is decided by evidence, budget, and
whether you already replied.

## Scope, stated up front

This is a **single-user, single-conversation, Chinese-language baseline**. Not a
general-purpose multilingual gate.

- All prompts, hook templates, rejection reasons and burden vocabulary are
  Chinese. The question-mark regex matches both `?` and `？`, but that is the
  only place language was considered.
- `conversations/*.jsonl` is read as one merged stream with no conversation id.
  If your directory holds several threads, a user message in thread B counts as
  "you replied" for a proactive message sent in thread A. Run one instance per
  thread if that matters to you.
- Timestamps without a timezone are interpreted in `policy.timezone`.

These are limits, not bugs — but they are the kind of thing you should know
before handing this to someone with a different setup.

## What this does not do

- It is not an agent framework. It does not call models to think.
- It does not decide *what* to say in any creative sense — an external generator does that.
- It does not sync memory across machines. See `examples/deployment/`.

## The shape

```
hooks → score → hard gates → dedupe → generate → SEND / DEFER / SILENT
```

Every decision is appended to `decisions.jsonl` with a reason. Silence is a
first-class outcome, not a failure.

### Hard gates (any one blocks sending)

| Gate | Default |
|---|---|
| Quiet hours | 23:00–09:00 |
| Daily cap | 1 message |
| Rolling 7-day cap | 3 messages |
| Last message on either side | defer until 13 min of quiet |
| Last proactive message unanswered | silent for 72 h |
| Consecutive unanswered | pause after 2 |
| No candidate above threshold | silent |

> **On "last message on either side".** An earlier version deferred whenever the
> *user* had said anything in the last 90 minutes. In practice that meant: the
> more you talk to someone, the less the gate ever opens — a chatty day produced
> zero messages, which is exactly backwards. What matters is whether a
> conversation is *live right now*, not whether the user has been online. So the
> clock starts from the most recent message from **either** side.

### Scoring

| Component | Weight |
|---|---|
| Relevance (Jaccard 2-gram against recent user messages) | 0.35 |
| Specificity | 0.25 |
| Timeliness | 0.20 |
| Novelty | 0.10 — **inactive by default**, see below |
| Chatability (28-day, 2-hour buckets) | 0.10 |
| Reply burden | −0.30 |

Threshold 0.55. A candidate scoring 0.55–0.75 gets an activity bonus; ≥ 0.75
sends directly.

These weights are **empirical defaults**, not validated optima. Treat them as a
starting point you are expected to change. They are read from `[policy.weights]`.

> **Novelty is currently inactive.** To measure how similar a candidate is to
> what you already sent, you need the *content* of past messages. This project
> deliberately keeps only a 12-char SHA-256 prefix (`hook_id`) of each sent
> hook, because storing the text of messages that were never sent — or were sent
> — turns an audit log into a transcript. With only a fingerprint you cannot
> compute Jaccard similarity, so the novelty term contributes 0 instead of
> silently contributing a constant 1.0 to every candidate.
>
> If you want real novelty scoring, add a local content index and accept that
> it holds plaintext. The honest position is: **no novelty score beats a fake
> one.**

## Quick start (no API key needed)

```bash
git clone https://github.com/YOU/agent-proactive-gate
cd agent-proactive-gate
python3 -m proactive_gate --config examples/config.toml --dry-run
```

`--dry-run` prints the decision and the reason, writes nothing, and consumes no
quota. It never calls the generator or the sender.

One known difference from a real run: `--dry-run` does **not** apply quiet
hours, so it can print `SEND` at 3 a.m. where a real run would be `SILENT`.
That is intentional — you asked for the decision, not for the schedule — but it
is a difference, so it is written down here rather than discovered later.

## Wiring a real generator

The generator is an external command: it receives the hook as its only argument
and prints one line of text — or `--` to decline this round.

```toml
[generator]
command = ["my-agent", "--one-shot"]
timeout = 200
env_file = "./.env.generator"   # optional; keeps API keys out of the config
```

## Wiring a real sender

```toml
[sender]
command = ["my-send-script"]
timeout = 120
```

The sender is only invoked when the decision is `SEND` **and** the generator
produced text. A non-zero exit is recorded but does not mark the message sent.

The generator is held to a strict contract, because a loose one sends garbage:
its exit code must be 0, its stdout must contain **exactly one** non-empty line,
that line must not be `--`, must not exceed `max_text_len` (default 300), and
must not contain control characters. Anything else is treated as "nothing to
say". Earlier versions took the last line of stdout regardless of exit code —
which meant a crashing generator could have its traceback delivered as a
message.

## Layout

```
proactive_gate/
  __main__.py     # CLI + decision loop
examples/
  config.toml
  synthetic-memory/   # fake facts/lessons/diary/conversations for demos
  deployment/         # systemd timer + memory-sync recipe
tests/
```

## Data it reads

Plain files, so you can point it at whatever you already have:

- `facts.md` — bullet list, `- **title**: body`
- `lessons.md` — same
- `diary/YYYY-MM-DD.md` — Markdown body
- `conversations/YYYY-MM-DD.jsonl` — `{"role","content","occurredAt"}`

Field names are configurable; see `[inputs]` in the example config.

## Privacy

- Nothing leaves the machine except through **your** `generator` and `sender`
  commands.
- `decisions.jsonl` stores a **hook fingerprint** (SHA-256 prefix), the hook
  length, the mode, the source *kind*, and the score components. It does not
  store the hook text or the candidate text, so a `SILENT` decision cannot leak
  what it almost said.
- The generated message *is* stored once (`text`) because you need to know what
  was sent.
- `.gitignore` excludes `state/`, logs, `.env*`, and the default memory inputs
  (`memory/`, `diary/`, `conversations/`).
- Note: hook text is passed to the generator as a **command-line argument**. On
  a shared host, process listings may expose it. There is no temp-file transport
  yet.

If your generator is a hosted model, whatever it receives leaves your machine.
Read `docs/privacy.md` before pointing this at a real diary.

## Known limitations

- `fcntl` file locks → **Linux only**. Windows fails at import.
- `tomllib` → Python **3.11+**. `pyproject.toml` says `>=3.11`; the module
  docstring said 3.12 — treat 3.11 as the floor.
- `ZoneInfo` needs tzdata present. Slim containers may not have it.
- Atomic state writes + a file lock do **not** give you exactly-once delivery.
  If your send channel has no idempotency key, a crash between send and state
  update can produce a duplicate.
- Jaccard is a cheap baseline, not semantic relevance.
- The 28-day activity estimate says when you *have* talked, not when you
  *want* to be interrupted.
- The question-mark rule is a surface heuristic. Text without `?` can still be
  demanding.

## Status

Early. See `TODO.md` for what is required before this should be called stable.

## License

MIT. See `LICENSE`.
