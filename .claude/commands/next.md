---
description: Pick the next unchecked ROADMAP item (or the one given) and run the full plan → build → review → document loop
argument-hint: "[optional roadmap item id, e.g. 1.1]"
---

Target item: $ARGUMENTS (if empty, the first unchecked item in ROADMAP.md, respecting phase order).

1. Read CLAUDE.md, ROADMAP.md, docs/facts.md, and anything the item depends on.
   If a prerequisite item is unchecked, say so and work on the prerequisite instead.
2. Write a short plan (files to touch, tests to add, acceptance criteria restated, what must
   be verified from primary sources). Create a task list from it.
3. Delegate implementation to the owning subagent named in the roadmap. Give it the plan,
   the acceptance criteria, and the relevant file paths.
4. Run `uv run pytest -q`. Fix failures (via the specialist) until green.
5. Send the diff and any results to `red-team-reviewer`. If FAIL, loop back to step 3 with
   its findings. Maximum 3 review rounds, then stop and summarise the blocker for Berk.
6. If the item changed any model or math, ask `quant-tutor` for a derivation note.
7. Update docs/facts.md with anything verified, tick the ROADMAP item with a link to the
   review file, and commit with a clear message.
8. Report to Berk: what was done, review verdict, open risks, and the next item.
