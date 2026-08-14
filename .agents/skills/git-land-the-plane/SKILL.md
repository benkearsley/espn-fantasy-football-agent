---
name: git-land-the-plane
description: End-of-session Git workflow for staging, committing, pushing, and closing out work when a repo is ready to land. Use when the user wants to wrap up a session, clean the working tree, sync Beads, create a commit, push to the remote, or verify that the repository is left in a landed state.
---

# Git Land The Plane

## Overview

Use this skill to finish a coding session cleanly. It turns an in-progress repo state into a deliberate landing: review what changed, stage the right files, make a focused commit, push to the remote, and confirm the repo is left in a clean, known state.

## Workflow

1. Inspect the repo state first.
   - Check branch, upstream, status, and any untracked or modified files.
   - Identify whether the current work is safe to land or whether it needs user input.

2. Keep the commit scoped.
   - Stage only the files that belong to the current session.
   - Exclude secrets, credentials, caches, generated binaries, and unrelated work.
   - If Beads is in use, sync issue state before landing.

3. Write a commit message from the session outcome.
   - Prefer a short, factual message describing what changed.
   - Add co-author attribution only when it is actually appropriate.

4. Push and verify.
   - Push the landing commit to the correct remote branch.
   - Confirm the branch is synchronized and the tree is clean.
   - If the repo is not ready to land, stop and report the blocker instead of forcing it.

## When To Use

Use this skill when:

- the user says to commit, push, land, or wrap up the session;
- the repo needs a clean end-of-session Git workflow;
- you need a repeatable landing routine that preserves task tracking and remote sync.

## Guardrails

- Do not stage or commit unrelated changes.
- Do not push if the branch state is ambiguous or unsafe.
- Do not treat this as a substitute for repo-specific release or review procedures.
