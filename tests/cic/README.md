# Claude-in-Chrome verification scripts

Browser checks run by an agent driving a real Chrome session, for the class of
failure that headless assertions do not catch.

## Why these exist alongside Playwright

`tests/e2e_smoke.mjs` is the regression gate: deterministic, fast, runs in CI, fails
loudly. It stays the primary automated check and every issue should add to it where
it can.

But the problems found in the July 2026 UI review were mostly *not* assertion
failures. The page rendered, the API returned 200, no console error fired — and the
experience was still wrong:

- the article list was blank for 10+ seconds while an LLM wrote a summary
- the time-range chip said 3h while the URL said 24h
- the top-ranked "important" story was a celebrity item, above a section index page
- link previews rendered as a bare URL

Those need a reader, not a matcher. These scripts are that reader, written down so
the judgement is repeatable and reviewable rather than a one-off impression.

## Format

One file per issue: `NNN-short-name.md`, where `NNN` matches the issue number.

Numbered steps. **Every step carries exactly one `EXPECT:` line.** Write for an agent
with no repo context — name full URLs, quote the exact text to look for, and say what
counts as failure. If a step needs a specific viewport, say so.

```markdown
# 004 — Deep links reproduce the sender's view

Target: http://localhost:8016  (prod: https://gdeltmonitor.com)

1. Load `{TARGET}/?view=geopolitics-conflict&hours=24`
   EXPECT: the "24h" chip is visually active and "3h" is not.

2. Read the article count line above the feed.
   EXPECT: it reports a count consistent with a 24-hour window, not a 3-hour one.

3. Click the "7d" chip, then press the browser Back button.
   EXPECT: the "24h" chip is active again and the feed matches step 1.

FAIL if: any chip state contradicts the URL, or Back does not restore prior state.
```

## Running one

Ask Claude to run the script against the dev instance:

> Run `tests/cic/004-deep-links.md` against http://localhost:8016 and report each
> step as PASS or FAIL with the observation that decided it.

The agent must report the *observation*, not just the verdict — "the 3h chip has the
active background, 24h does not" rather than "FAIL". A verdict with no observation
behind it is not a result.

## Rules

- **Dev first.** Run against `:8016`. Only run against production for a post-deploy
  confirmation, and never run a script with side effects there.
- **No destructive steps.** These scripts read and click; they do not delete, submit
  payment, or change account settings. Anything that writes needs an explicit note in
  the script saying what it writes and how to undo it.
- **A script that cannot fail is not a test.** If every step would pass on today's
  broken build, the expectations are too loose.
- **Paste the transcript into the PR.** An issue labelled `needs-cic` does not close
  until its script has been run and the step-by-step result recorded.
- Keep scripts current. If a change makes an expectation obsolete, update the script
  in the same PR — a stale script is worse than none.
