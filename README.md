# Support Engineering Portfolio

Two artifacts from a technical assessment for a digital asset custody platform.
They're here because they show how I work rather than what I've read: one is an
operations script, the other is a runbook a support team could adopt as-is.

Company-specific details have been generalized.

---

## `runbooks/transaction-failure-triage.md`

A troubleshooting guide for support agents handling "why did my transaction
fail?" on an MPC custody platform.

The reason it exists in this form: **"failed" is not one event.** A transfer
passes through several independent systems — policy evaluation, compliance
screening, multi-party signing, broadcast, confirmation — and the answer depends
entirely on where it stopped. A transaction blocked by the policy engine and one
that reverted on-chain look identical to the customer ("it didn't work") and have
nothing in common technically.

So the guide doesn't open with a list of causes. It opens with a **triage step**:
locate the transaction in its lifecycle, read its state, then go to the matching
section. Everything after that is organized by where the failure lives —
network, wallet and signing, API and integration, compliance and policy,
infrastructure.

Each failure mode carries five things:

- Symptoms as customers actually report them, not as engineers describe them
- Root cause, technical but explainable
- Troubleshooting steps an agent can follow
- A customer message template
- Explicit escalation criteria

A few decisions worth calling out:

**Funds first.** Every customer-facing template answers "did my money move?"
before anything else, because that is what the customer is actually asking.
Telling someone their funds are gone when the transaction never left the policy
engine is the most damaging mistake an agent can make on this kind of platform.

**Compliance has a hard boundary.** Agents report the outcome and route the
case. They don't explain, adjudicate, or speculate about screening decisions —
in several jurisdictions, warning someone that their transaction was flagged
carries legal exposure. The guide separates this from a screening service that
is *failing*, which is an engineering problem, not a compliance outcome.

**Recognizing incidents.** Two customers reporting the same chain-specific
symptom means stop working tickets and raise an incident. That rule is written
down rather than left to instinct.

---

## `scripts/update_stuck_transactions.py`

A Python operations script that finds transactions stuck in `PENDING` past a
threshold and moves them to `FAILED` so customers can retry. Tested against a
local PostgreSQL instance.

The interesting part isn't the update — it's what surrounds it:

**Race condition handling.** The `SELECT` and `UPDATE` run in a single
transaction with `FOR UPDATE SKIP LOCKED`. Without the row lock, a payment
processor completing a transaction between the read and the write would get
overwritten with `FAILED` — telling a customer their payment failed when it
actually succeeded.

**Timezone correctness.** `created_at` is `TIMESTAMP` without time zone, so the
script compares against `NOW() AT TIME ZONE 'UTC'` rather than `NOW()`. Plain
`NOW()` casts using the session time zone, which shifts the 24-hour cutoff
depending on where the script runs — a real problem for a team split across
regions.

**Dry run by request.** `--dry-run` reports exactly what would change and rolls
back. This is how anything touching production financial data should be run the
first time.

**Blast radius.** A batch limit caps how many rows one execution can modify, and
warns when the limit is hit. A mistyped threshold shouldn't be able to fail
forty thousand transactions.

**Audit accuracy.** `RETURNING` means the log reflects what the database
confirms was written, not what the script assumed.

Roughly half the file is error handling, validation, logging, and resource
cleanup. That ratio is deliberate: a script that modifies transaction states runs
on a schedule with nobody watching, and a silent failure leaves financial data
inconsistent.

### Running it

```bash
# Preview — commits nothing
python3 update_stuck_transactions.py --dry-run

# Apply
python3 update_stuck_transactions.py

# Different threshold and batch size
python3 update_stuck_transactions.py --hours 48 --limit 1000
```

Connection details come from `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, and
`PGPASSWORD`. Requires `psycopg2-binary`.

Safe to run repeatedly — a second execution against an already-processed set
reports nothing to do and exits cleanly.

---

## About me

Support engineer, 10+ years in enterprise IT support, systems integration, and
automation. Previously at OTRS, HCL Technologies, and Ericsson. Based in Mexico
City, working in English and Spanish.

Currently focused on digital asset infrastructure — I test on/off-ramp providers
hands-on and document how they fail in practice, which providers stall on
withdrawals and when an alternative route is needed.

GitHub: [@leovaz](https://github.com/leovaz)
