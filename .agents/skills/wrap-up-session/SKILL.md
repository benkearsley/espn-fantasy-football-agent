---
name: wrap-up-session
description: Session-close orchestration for this repository. Use when the user wants to finish work, review any CANONICAL_SPEC.md changes made during the session for legitimacy and surgical scope, record important findings or behavior patterns in the notes directory, and then land the repo with git-land-the-plane.
---

# Wrap Up Session

## Overview

Use this skill to close out a session in this repo without mixing responsibilities. It checks whether the canon spec really needs a change, writes durable findings to `notes/` when there is something worth preserving, and then hands off to `git-land-the-plane` for the actual Git landing workflow.

## Workflow

1. Review the session output.
   - Look for anything that would justify a surgical update to `CANONICAL_SPEC.md`.
   - Review any actual `CANONICAL_SPEC.md` diff from the session and confirm the change is legitimate, narrow, and direction-setting.
   - Assume the spec stays unchanged unless the direction truly changed.

2. Protect the canon.
   - Only modify `CANONICAL_SPEC.md` if the change is deliberate, narrow, and clearly direction-setting.
   - Keep implementation details, tactics, and one-off decisions out of the spec.

3. Record findings.
   - If the session produced important findings, decision patterns, or behavioral observations, write them into the `notes/` directory as a session log.
   - Keep the note factual and durable.

4. Land the plane.
   - Once the spec decision and notes step are complete, invoke `git-land-the-plane`.
   - Do not duplicate the Git landing workflow here; defer to that skill.

## Final Output

When the wrap-up is complete, respond like a landing summary:

- one concise summary of the actions taken;
- pointers to the `CANONICAL_SPEC.md` change if one was made;
- pointers to the notes file if one was written;
- mention the landing step only by reference to `git-land-the-plane`, not by re-describing it.

## When To Use

Use this skill when wrapping up work in this repository and you need the end-of-session sequence to be explicit:

- decide whether the canon spec should change;
- write session findings into notes;
- then land the repo through the dedicated Git skill.

## Guardrails

- Do not use this skill to broaden or rewrite the canon spec casually.
- Do not put implementation notes into the spec.
- Do not repeat the `git-land-the-plane` workflow here.
