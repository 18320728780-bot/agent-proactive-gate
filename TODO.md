# Before calling this stable

## Config & interface
- [ ] `tomllib` config: input dirs, state dir, log dir fully separate
- [ ] local fake generator + fake sender — run end-to-end with zero credentials
- [x] split generator command from sender command
- [ ] CLI: `evaluate`, `--dry-run`, `--config`
- [x] `--dry-run` must not send, not consume quota, not pollute dedupe state
- [ ] external commands via argv arrays, never `shell=True`
- [x] state the Linux-only `fcntl` dependency; do not advertise cross-platform

## Correctness
- [ ] define when a `DEFER` is retried and when it expires
- [ ] define which timezone "a day" means; define the rolling-7d window precisely
- [x] count a message as sent only after the sender succeeds
- [ ] two overlapping timer runs must not both pass the budget check
- [ ] define behavior when the send times out but the remote may have received it
- [ ] stable hook IDs — moving or re-reading a file must not bypass dedupe
- [ ] defined behavior for a truncated last log line or a missing state file
- [x] atomic write + file lock
- [ ] document that atomic write + lock ≠ exactly-once delivery

## Tests
- [ ] `unittest`, standard library only
- [ ] fixed clock: midnight, day rollover, quiet hours, 48h boundary
- [ ] 90-minute cooldown, daily cap, rolling 7-day cap
- [ ] unanswered-last, same hook, same mode, question-ratio rules
- [ ] generator failure, sender failure, timeout, duplicate run
- [ ] synthetic history fixture that demonstrates SEND / DEFER / SILENT
- [ ] offline replay printing the decision distribution

## Privacy
- [x] inputs and decision logs gitignored by default
- [x] log IDs, scores and reasons — not full private text
- [ ] document what reaches the external generator
- [ ] support a denylist of files/topics that must never become hooks
- [ ] treat diary/chat content as data, never as instructions
- [ ] provide a pause / disable / purge path

## Open design question
- [ ] The question-mark rule currently runs at dedupe time, i.e. **before**
      generation — but whether the final text contains `?` is only known
      **after** generation. Either move the check after generation or drop it.
