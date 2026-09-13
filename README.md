# PromiseLedger

A Linear ticket marked **Done** is a claim. A merged GitHub pull request or a sent customer email is proof.

PromiseLedger is a multi-step agent that re-reads **Linear**, **GitHub**, **Gmail**, and **Slack**, joins on immutable IDs (never a person's name), remediates with bounded writes, and leaves customer mail as an **unsent Gmail draft**. Slack is never the source of truth. There is no LLM in the loop: classification, writes, and grading are code.

This is the Multi-App AI Agent Hackathon submission.

## 1. Overview

Engineering and CS teams mark tickets Done while the backing work never landed. The agent:

1. Snapshots the four apps
2. Builds a promise ledger with exact ticket-key joins (`PL-102`)
3. Classifies HEALTHY / DESYNC / BREACHED / DONE_UNRECORDED
4. Writes only the allowed remediation (reopen, evidence comment, unsent draft, Slack digest)
5. Re-reads. HTTP 200 with no state change is a failure
6. An **independent grader** scores final app state, not the agent report

Ambiguous identity (two people named Alex Kim, missing customer ID) → **zero writes**.

## 2. External apps

| App | Role | Writes | Forbidden |
|---|---|---|---|
| Linear | Ticket claim | reopen / close / breach + evidence comment | deletes, invented IDs, agent-minted waivers |
| GitHub | Proof of merged work | none (read-only) | comments, merges, repo mutation |
| Gmail | Sent notice is proof; new mail is review | **draft create only** | send, reply, guessed recipient |
| Slack | Operator digest | one idempotent digest after read-back | treating Slack as truth |

Fixture adapters implement the same interface as live APIs, including injected `HTTP 200` no-ops. Live adapters exist for disposable workspaces (`run-live`). The scored reliability suite needs **no credentials**.

## 3. Setup

Python 3.11+, no third-party packages.

```bash
python -m unittest discover -s tests -v
python -m promiseledger.cli eval --output .runs/eval
python -m promiseledger.demo
# open http://127.0.0.1:8787
```

Demo cases for the video:

- **s2-desync** — Linear Done, no PR, Slack already said shipped → reopen + unsent draft
- **s6-ambiguous-contact** — two Alex Kims → refuse, world unchanged
- **s10-silent-200** — Linear returns 200 but does not reopen → grader fails

Live mode (optional, disposable workspace only): copy `.env.example`, then `python -m promiseledger.cli run-live --output .runs/live`.

## 4. Reliability testing

`promiseledger/grader.py` compares initial vs final snapshots and rebuilds the ledger itself.

| ID | Seed | Expected |
|---|---|---|
| s1-healthy | Done + merged PR + sent notice | pass, proven no-op |
| s2-desync | Done, no proof | pass, reopen + draft |
| s3-done-unrecorded | PR merged, ticket still open | pass, close with evidence |
| s4-breached | overdue, no PR | pass, breach + draft |
| s5-missing-notice | Done, required sent email missing | pass, reopen + draft |
| s6-ambiguous-contact | two same-name contacts | pass by refusal |
| s7-replay | remediable case, run twice | second run adds zero writes |
| s8-neighbor-freeze | bad ticket beside a healthy neighbor | neighbor hash unchanged |
| s9-missing-id | customer ID points at nobody | pass by refusal |
| s10-silent-200 | Linear 200, status still Done | **expected fail** (G3/G6) |

`eval` succeeds only when ordinary scenarios pass **and** s10 is rejected by the grader.

CI: `.github/workflows/test.yml` runs unittest + eval on every push.

## 5. Demo

Local console: `python -m promiseledger.demo` → http://127.0.0.1:8787

**≤2 min video:** _paste public URL here before submitting the form._

Storyboard: 0:00 invariant; 0:15 s6 refusal; 0:40 s2 reopen + unsent draft; 1:10 s10 silent 200; 1:30 grader table; 1:50 replay / neighbor freeze.

## Architecture

This is a **tool-calling agent**, not a chatbot. There is no LLM in the loop because ticket IDs, customer IDs, and pass/fail must be exact.

```
snapshot four adapters (fixture or live HTTP, same interface)
        → join on PL-102 / customer_id / email (never a name)
        → classify with a pure function
        → bounded writes: Linear status+comment, Gmail DRAFT only, Slack digest
        → read-back (HTTP 200 is not success)
        → independent grader on final app state
```

Live adapters (`promiseledger/adapters/live.py`) call Linear GraphQL, GitHub REST, Gmail drafts, and Slack `chat.postMessage`. The public demo uses the **same planner** against isolated worlds so anyone can reproduce it without putting an inbox token on a public URL. `send` is not implemented. GitHub is read-only.

Hosted console: https://promiseledger.vercel.app
