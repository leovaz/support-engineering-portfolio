# Transaction Failures — Support Troubleshooting Guide

**Audience:** Support agents handling "why did my transaction fail?" tickets
**Scope:** MPC-based digital asset custody platform — vaults, wallets, policy engine, signing, broadcast
**Owner:** Support Tech Lead

---

## How to use this guide

Most mis-handled tickets come from one mistake: treating "failed" as a single
event. In an MPC custody platform a transfer passes through several independent
systems, and the answer to "why did it fail?" depends entirely on **where it
stopped**. A transaction that never left the policy engine and a transaction
that reverted on-chain look identical to the customer — "it didn't work" — but
they have nothing in common technically, and the customer messages are opposite.

So the first step is never "check gas prices." The first step is **locate the
transaction in its lifecycle**, then open the matching section below.

---

## Step 0 — Locate the failure (do this before anything else)

```
  1. INITIATION          API request accepted, transaction created
         │                   ├── fails here → §3 API & Integration
         ▼
  2. POLICY & AML        Vault policy evaluated, AML screening runs
         │                   ├── fails here → §4 Compliance & Policy
         ▼
  3. APPROVAL            Required signers vote on the request
         │                   ├── stalls here → §2 Wallet & Security
         ▼
  4. MPC SIGNING         Key shares combine to produce a signature
         │                   ├── stalls here → §2 Wallet & Security
         ▼
  5. BROADCAST           Signed transaction submitted to the network
         │                   ├── fails here → §1 Network-Level
         ▼
  6. CONFIRMATION        Included in a block, CONFIRMED
                             └── stuck here → §1 Network-Level
```

**Retrieve the facts first:**

```
GET /v2/vaults/{vault_id}/transactions/{transaction_id}
```

Then read, in this order:

| Field | What it tells you |
|---|---|
| `state` | Where it stopped. `AWAITING_POLICY_CHECK` means it never reached the network. `CONFIRMED` means it succeeded regardless of what the customer thinks. |
| `subType` | Whether it is a native transfer, token transfer, contract call, etc. |
| `request.initiator` | Who started it — a user, or a service account. |
| `designatedSigners` | Whether it is waiting on one specific person. |
| `signingSession` | Empty means MPC signing has not begun. |
| `evmTransaction.fee` | `gasLimit`, `maxFeePerGas`, `maxPriorityFeePerGas`, `gasUsed`. |
| `evmTransaction.data` | Present on token transfers and contract calls. |
| `createTime` | Age. Anything older than ~1 hour with no state change is abnormal. |

Supporting endpoints:

- `GetTransactionAMLScreening` — compliance outcome
- `GetLatestTransactionSimulation` — pre-flight balance changes, if simulation ran
- `EstimateTransactionFee` — current fee estimate for comparison

> **Critical distinction — never confuse these two:**
> A transaction that **never got a signature** does not exist on-chain. Nothing
> was spent, nothing was lost, and the customer can simply retry. A transaction
> that was **broadcast and reverted** consumed gas but did not move the asset.
> Telling a customer "your funds are gone" when the transaction never left the
> policy engine is the single most damaging error an agent can make on this
> platform.

---

## Reference: platform error codes

The platform's API returns gRPC status codes plus platform-specific codes.
The codes below are illustrative of the categories an agent should recognise
on sight; the exact numbers vary by platform:

| Code | Meaning | Section |
|---|---|---|
| `1001` | `AUTHENTICATION_TOKEN_EXPIRED` | §3.2 |
| `1002` | `AUTHENTICATION_TOKEN_MISSING_OR_INVALID` | §3.2 |
| `10001` | `TRANSACTION_INSUFFICIENT_FUNDS` | §2.1 |
| `10002` | `TRANSACTION_EXECUTION_REVERTED` | §1.4 |
| `10003` | `TRANSACTION_SOURCE_ACCOUNT_NOT_ACTIVATED` (notably TRON) | §1.3 |
| `10004` | `TRANSACTION_ALREADY_SIGNED` | §3.4 |

---

# §1 Network-Level Failures

The transaction was signed and broadcast. Everything inside the platform worked. The
blockchain is where the problem lives.

## 1.1 Insufficient Gas Fees / Network Congestion

**Symptoms customers report**
- "It's been pending for hours"
- "I can see it on Etherscan but it never confirms"
- "It worked yesterday with the same settings"

**Root cause**
Blockchains order transactions by fee. When demand spikes, the price required
for inclusion in the next block rises. A transaction signed at a fee that was
generous an hour ago can sit unmined indefinitely if the market moved. Nothing
is wrong with the transaction — it is queued in the mempool, valid, waiting for
a block that will accept its price.

Important: the funds are **not** spent. They are reserved but still in the
wallet until the transaction is mined.

**Troubleshooting**
1. Confirm the transaction reached the network: it has a hash and appears on the
   block explorer.
2. Read `evmTransaction.fee.maxFeePerGas` from the transaction.
3. Call `EstimateTransactionFee` for the same transfer now, and compare. If the
   current estimate is meaningfully above what was signed, this is the cause.
4. Check the network's current base fee independently (Etherscan Gas Tracker or
   equivalent) to confirm it is a market-wide condition and not a single stuck
   transaction.
5. Resolve with **`ReplaceTransaction`** — this re-broadcasts with a higher fee
   using the same nonce, which is the correct fix. Do not tell the customer to
   "send it again": a second transfer at a new nonce can result in *both*
   confirming and the payment going out twice.
6. If the customer wants out entirely, use `CancelTransaction`.

**Customer message template**
> Hi [Name],
>
> Your transfer of [amount] was signed and submitted successfully — the delay is
> on the [network] network itself, which is unusually busy right now. The fee
> attached when we submitted is currently below what miners are accepting, so it
> is waiting in the queue rather than failing.
>
> Your funds have not left your wallet and nothing has been lost. We can
> re-submit the same transfer with a higher fee to get it confirmed, or cancel it
> and return it to your available balance. Which would you prefer?
>
> [Agent], Support

**Escalate to engineering when**
- `EstimateTransactionFee` is returning errors, stale values, or estimates that
  are obviously wrong for current conditions.
- `ReplaceTransaction` has been attempted twice at increasing fees and the
  transaction still is not mined.
- Multiple customers report stuck transactions on the same network within a
  short window — that is a platform-level fee estimation issue, not a customer
  issue, and it needs an incident, not five tickets.

## 1.2 Nonce Gaps and Ordering (EVM)

**Symptoms**
- "My newer transactions confirmed but an older one is still pending"
- Several transfers from the same wallet all stuck together

**Root cause**
EVM chains execute transactions from an address strictly in nonce order. If
nonce 41 is stuck, nonces 42 and 43 cannot confirm no matter how high their fee
is. One stalled transaction blocks the queue behind it.

**Troubleshooting**
1. List all pending transactions for the source wallet, sorted by creation time.
2. Identify the **oldest** pending one — that is the blocker.
3. Apply `ReplaceTransaction` to the blocker, not to the newer ones.
4. Confirm the queue drains behind it.

**Customer message template**
> Hi [Name],
>
> The three transfers you mentioned are all waiting on the earliest one. On
> Ethereum, transactions from the same wallet confirm strictly in order, so once
> the first clears the others will follow automatically.
>
> We've re-submitted the earliest one with a higher fee. I'll confirm here as
> soon as the queue clears.

**Escalate when**
- The blocking transaction cannot be replaced or cancelled.
- The nonce sequence has a gap that does not correspond to any transaction
  visible in the vault — that suggests something was submitted outside the
  platform, and needs investigation.

## 1.3 Network-Specific Requirements

Not every chain behaves like Ethereum. These are the recurring ones:

| Chain | Requirement | Typical failure |
|---|---|---|
| TRON | Destination account must be activated; bandwidth/energy consumed instead of a simple fee | `10003 TRANSACTION_SOURCE_ACCOUNT_NOT_ACTIVATED` |
| Bitcoin (UTXO) | Spends whole UTXOs; needs sufficient confirmed inputs | Fails despite an apparently sufficient balance |
| Solana | Requires rent-exempt minimum; blockhash expires quickly | Signed transaction expires before landing |
| Stellar | Destination needs an account and a trustline for non-native assets | Payment rejected at the protocol level |
| XRP | Destination tag often required by exchanges | Funds arrive but are not credited |

**Root cause (customer-friendly)**
Each blockchain has its own rules about what an account must have before it can
receive or send. These are protocol requirements, not platform restrictions.

**Troubleshooting**
1. Identify the network from the transaction's `network` field.
2. Check the chain-specific precondition from the table above.
3. For Bitcoin, use `QueryWalletUTXOs` — an available balance made up of many
   tiny or unconfirmed UTXOs may not be spendable in one transfer.
4. For Stellar/XRP, verify the destination's account state and any required
   memo/tag with the receiving party before retrying.

**Customer message template**
> Hi [Name],
>
> This one is a rule specific to the [network] network rather than a problem with
> your account. [Plain-language explanation of the requirement.]
>
> Here's what needs to happen before the transfer can go through: [specific
> step]. Once that's done I'll re-run it for you and confirm.

**Escalate when**
- The precondition appears satisfied and the transaction still fails.
- A chain starts producing this error where it never did before — likely a
  protocol upgrade the platform needs to account for.

## 1.4 On-Chain Execution Reverted

**Symptoms**
- "It says failed but I was still charged"
- Transaction is on the explorer, marked failed, gas was consumed

**Root cause** — error `10002 TRANSACTION_EXECUTION_REVERTED`
The transaction was included in a block but the contract rejected it during
execution. Gas is paid for the computation attempted, so the fee is consumed
even though the transfer did not happen. Common triggers: insufficient token
allowance, a token contract with transfer restrictions (pausable, blacklisted
address), slippage limits on a swap, or a gas limit too low for the operation.

**Troubleshooting**
1. Get the revert reason from the block explorer or the node response.
2. If it is a token approval issue, check whether an allowance step is missing.
3. Check whether the token contract is paused or restricts the destination —
   some regulated stablecoins do freeze addresses.
4. Compare `gasLimit` against `gasUsed`. Hitting the limit exactly means the
   limit was too low.
5. Re-run with `runSimulation: true` before retrying so the same revert is
   caught before it costs gas again.

**Customer message template**
> Hi [Name],
>
> This transfer was accepted by the network but rejected by the token's own
> contract when it ran. Because the network still did the work of processing it,
> the gas fee was charged even though the tokens didn't move — that's how the
> blockchain works, not a fee we've applied.
>
> The specific reason was: [plain-language revert reason]. Here's what we need to
> change before retrying: [action].

**Escalate when**
- The revert reason is not recoverable from the node or explorer.
- The same contract reverts repeatedly for multiple customers.
- A revert occurs on a straightforward native transfer — that should not happen
  and indicates something wrong in transaction construction.

---

# §2 Wallet & Security Failures

The transaction never reached the network. Something inside the vault, the key
material, or the approval chain stopped it.

## 2.1 Insufficient Balance

**Symptoms**
- "I have enough, why does it say I don't?"
- Fails immediately at initiation

**Root cause** — error `10001 TRANSACTION_INSUFFICIENT_FUNDS`
Three distinct situations get reported identically:

1. **Genuinely short** on the asset being sent.
2. **Enough of the token, no native coin for gas.** This is the most common one.
   Sending USDC on Ethereum requires ETH in the same wallet to pay gas. A wallet
   holding 10,000 USDC and zero ETH cannot send anything.
3. **Balance is displayed but not spendable** — pending outbound transactions
   have already reserved it, or on Bitcoin the UTXOs are unconfirmed.

**Troubleshooting**
1. `QueryWalletBalances` for the source wallet — check both the asset **and** the
   network's native coin.
2. `EstimateTransactionFee` — compare the native balance against the estimate.
3. Call `RefreshAssetAddressBalance` if the displayed balance looks stale after a
   recent deposit.
4. List pending outbound transactions from the same wallet that may have
   reserved funds.
5. If it is a gas shortfall, check whether a **Gas Station** is configured for
   that vault (`ListGasStations`) and whether it is itself funded. A depleted gas
   station causes this failure across every wallet it serves.
6. Consider a **sponsored transfer** where supported, which lets a separate
   account pay the gas.

**Customer message template**
> Hi [Name],
>
> Your USDC balance is fine — the issue is that the wallet doesn't hold any ETH.
> On Ethereum, moving any token requires a small amount of ETH in the same wallet
> to pay the network fee, similar to needing postage to send a package even when
> the package is already paid for.
>
> You'd need roughly [amount] ETH in [wallet] to complete this transfer. Once
> it's funded the transaction will go through normally — or I can look at whether
> gas sponsorship is an option for your setup.

**Escalate when**
- Balances still look wrong after `RefreshAssetAddressBalance`.
- The gas station is funded but transactions still fail on gas.
- Reported balance and on-chain balance disagree — that is a platform issue and
  should be raised immediately, not retried.

## 2.2 Signer and Device Availability

**Symptoms**
- "It's stuck waiting for approval"
- "I approved it but nothing happened"
- Sits in an awaiting-approval state indefinitely, then expires

**Root cause**
MPC signing requires the participating parties to be reachable and holding valid
key shares. Common blockers: the designated signer is unavailable, a signer lost
or replaced the device holding their key share, the signer's app is not
installed or is on an outdated version, or the approval quorum defined by policy
simply is not met yet.

This is the failure mode most often misdiagnosed as a network problem, because
the customer sees "pending" and assumes the blockchain.

**Troubleshooting**
1. Read `designatedSigners`. If populated, only that person can sign — the
   transaction is not stuck, it is waiting on one individual.
2. Check the vault's approval policy and how many approvals are required against
   how many have been recorded.
3. Verify the signer has an active device with a valid key share.
4. Confirm they are on a supported app or CLI version.
5. Check `expireTime` — if it has passed, the transaction is dead and must be
   re-initiated, not approved.
6. If a Co-Signer is expected to handle it automatically, verify the Co-Signer
   process is running and its webhook policy endpoint is reachable.

**Customer message template**
> Hi [Name],
>
> This transfer is waiting on an approval rather than on the network. Your
> security policy requires [N] approval(s), and we currently have [M]. The
> transfer is designated to [signer], so it needs their sign-off specifically.
>
> Nothing is at risk and no funds have moved. Once [signer] approves from their
> device it will go through immediately. If they're unavailable, we can look at
> whether another authorised signer can be designated instead.

**Escalate when**
- The signer confirms they approved but the state did not change.
- Signing starts (`signingSession` populated) and then stalls or errors.
- A device or key share appears invalid, unenrolled, or unrecoverable — **key
  material problems are always engineering, never a support retry.**

## 2.3 Co-Signer / Automation Not Signing

**Symptoms**
- Automated transfers stopped working with no configuration change
- Transactions accumulate awaiting a signature that never comes

**Root cause**
The Co-Signer signs automatically based on a webhook policy. If the process has
stopped, its key share is unavailable, its token has expired, or the policy
endpoint is unreachable, transactions pile up unsigned. Nothing errors loudly —
they just wait.

**Troubleshooting**
1. Confirm the Co-Signer process is running and check its logs.
2. Verify its service account credentials are valid and the JWT is not expired
   (see §3.2).
3. Confirm the webhook policy endpoint is reachable and returning 200.
4. Check whether the transactions being skipped fall outside the policy's
   auto-approval criteria — that would be correct behaviour, not a fault.
5. Check for a recent CLI upgrade or state migration that may have moved the
   key share location.

**Customer message template**
> Hi [Name],
>
> Your automated signing has stopped picking up new transactions. The transfers
> themselves are valid and queued safely — they're simply not being signed
> automatically.
>
> We're checking the co-signer service and its policy endpoint now. In the
> meantime these can be approved manually by any authorised signer if any of them
> are time-sensitive.

**Escalate when**
- The Co-Signer is running, credentials are valid, and it still does not sign.
- Its key share cannot be loaded.

---

# §3 API & Integration Failures

The request never became a transaction. These are the fastest to resolve because
the error is deterministic and reproducible.

## 3.1 Malformed Requests and Invalid Parameters

**Symptoms**
- `INVALID_ARGUMENT` from the API
- "The integration worked in sandbox but not in production"
- Transactions created with wrong amounts or to wrong addresses

**Root cause**
The most frequent offenders, in order:

1. **Wrong asset identifier.** The platform uses `assets/{type}.{network_id}[.{contract}]`.
   Using the ERC-20 form for a native coin (or vice versa) fails. Native ETH is
   `assets/native.ethereum-mainnet`, not an erc20 identifier.
2. **Invalid destination address.** Wrong length, invalid hex characters, or an
   address for a different chain. Worth stressing: an address that *looks*
   plausible can still be invalid — a single non-hex character in a 40-character
   string is easy to miss visually.
3. **Amount sent as a JSON number.** Amounts are decimal **strings** in whole
   units. Floating-point representation of financial values causes precision
   errors.
4. **Wrong `details` message.** `details` is a `oneof` — exactly one transaction
   type must be set.
5. **Malformed `expireTime`.** Must be RFC 3339 (`2025-06-10T12:30:05Z`).
6. **Invalid `requestId`.** Must be a valid UUID; the zero UUID is explicitly
   unsupported.

**Troubleshooting**
1. Get the exact request body and the full error response from the customer.
2. Validate the destination address format for the target chain first — this is
   both the most common error and the most costly.
3. Verify the asset identifier against the Asset Types reference.
4. Have them re-send with `"validateOnly": true` — validates without creating.
5. Add `"runSimulation": true` to surface execution problems pre-approval.

**Customer message template**
> Hi [Name],
>
> The API rejected this request before creating any transaction — no funds were
> involved at any point.
>
> The issue is in the [field] field: [specific problem and correct format].
>
> If it helps, sending the same request with `"validateOnly": true` will check the
> payload without creating a transaction, which is a safe way to confirm a fix
> before running it for real.

**Escalate when**
- A payload that matches the documentation is rejected anyway.
- The error message does not identify which field is at fault.
- A previously working integration starts failing with no client-side change —
  possible API regression.

## 3.2 Authentication Failures

**Symptoms**
- Integration worked, then stopped abruptly at a predictable interval
- `UNAUTHENTICATED` errors
- Errors `1001` or `1002`

**Root cause**
The platform authenticates with a bearer JWT signed by a service account's private key.
The documented example issues tokens valid for one hour. `1001` means the token
expired — usually a client that generates a token at startup and never refreshes
it, which works fine in testing and fails an hour into production. `1002` means
the token is missing, malformed, or signed with a key that does not match the
registered public key.

Also check: the service account's role must be **approved by a vault admin**
before it can be used, and a viewer role cannot initiate transactions.

**Troubleshooting**
1. Distinguish `1001` (expired — refresh logic) from `1002` (invalid — key or
   header problem).
2. Confirm the `Authorization: Bearer <token>` header is present and correctly
   formed.
3. Verify JWT claims: `sub` = service account email, `aud` = the platform's API audience,
   `exp` in the future, algorithm RS256.
4. Confirm the private key matches the public key registered in the console, and
   that PEM newlines survived whatever environment variable carried it — mangled
   `\n` sequences are a classic cause.
5. Confirm the service account role was approved and has signer (not viewer)
   permission.
6. Check whether the key was recently rotated.

**Customer message template**
> Hi [Name],
>
> Your API token has expired. Our access tokens are short-lived by design —
> it limits the damage if a token is ever exposed.
>
> The fix is to generate a fresh token before it expires rather than reusing one
> from startup. Most integrations handle this by generating a new token per
> request or refreshing on a timer well inside the expiry window.
>
> No transactions were affected — requests were rejected before reaching your
> vault.

**Escalate when**
- Credentials are confirmed correct and authentication still fails.
- Auth failures appear across multiple customers simultaneously.
- There is any indication of credential compromise — treat as a security
  incident immediately, not a support ticket.

## 3.3 Rate Limiting

**Symptoms**
- Intermittent failures under load
- `RESOURCE_EXHAUSTED`
- Bulk operations partially complete

**Root cause**
Request volume exceeded the permitted rate. Usually a batch job with no
throttling, an aggressive polling loop, or a retry storm where failed requests
are retried immediately, multiplying the load that caused the problem.

**Troubleshooting**
1. Identify the calling pattern — burst, or sustained?
2. Look for polling that should be webhooks. Polling `GetTransaction` every
   second across many transactions is the most common cause, and webhooks remove
   it entirely.
3. Check retry logic: immediate retries make rate limiting worse. Exponential
   backoff with jitter is required.
4. Recommend batch endpoints (`BatchGetTransactions`, `BatchGetWallets`) instead
   of per-item loops.

**Customer message template**
> Hi [Name],
>
> These failures are rate limiting rather than anything wrong with the
> transactions — your integration is sending requests faster than the limit
> allows, so some are being rejected.
>
> Two changes usually solve this permanently: subscribe to webhooks for
> transaction state changes instead of polling for them, and add exponential
> backoff to retries. I can share the webhook setup guide if useful.

**Escalate when**
- Limits are hit at volumes well within the documented allowance.
- The customer's volume legitimately requires a limit increase.

## 3.4 Duplicate Submissions

**Symptoms**
- "It charged me twice"
- Error `10004 TRANSACTION_ALREADY_SIGNED`

**Root cause**
A retry without a stable `requestId`. `requestId` is the platform's idempotency key —
retrying with the same one is recognised as the same request for at least 60
minutes. Retrying with a *new* UUID creates a genuinely new transaction, which
is how duplicate payments happen.

`10004` is different and usually benign: an attempt to sign something already
signed, typically two signing paths racing each other.

**Troubleshooting**
1. Search by `externalId` and `requestId` to establish whether one transaction
   or two actually exist.
2. Determine whether both were broadcast. If only one is on-chain, no duplicate
   payment occurred.
3. If two confirmed, this is a fund-recovery matter — escalate and involve the
   customer's finance contact. Do not attempt any correction from support.
4. Fix forward: the client must generate the `requestId` once, per logical
   payment, and reuse it across all retries of that payment.

**Customer message template**
> Hi [Name],
>
> I've confirmed what happened: [one transaction / two transactions] were
> created. [State plainly whether funds moved twice.]
>
> The cause is that the retry generated a new request ID. The platform uses that ID to
> recognise a retry of the same payment rather than a new one — reusing it across
> retries of the same payment prevents this entirely.
>
> [If duplicated:] I've escalated the recovery side and will come back to you
> with a concrete update by [time].

**Escalate when**
- Two transfers confirmed on-chain. Always. Immediately.

---

# §4 Compliance & Policy Failures

The transaction was blocked deliberately. These need the most careful handling —
compliance outcomes are often not fully explainable to the customer, and the
agent must not speculate about why.

> **Rule for agents:** never guess at or invent a compliance reason, and never
> tell a customer which specific screening signal triggered a block. Report the
> outcome, route the case, and let compliance decide what can be shared.

## 4.1 AML / Screening Blocks

**Symptoms**
- "It was rejected but nothing was wrong with it"
- Fails at policy check, never reaches the network
- Specific destination addresses consistently fail

**Root cause**
The platform runs AML screening on transactions, and the result carries an action such
as `ALLOW` or a block. Screening evaluates the counterparty address against risk
data — sanctions exposure, known illicit sources, mixer proximity. A block may
also arrive asynchronously, signalled by the
`TRANSACTION_AML_SCREENING_RESULT_READY` webhook.

**Troubleshooting**
1. `GetTransactionAMLScreening` for the transaction — record the action.
2. Confirm the transaction never reached the network. It did not; funds are
   untouched.
3. **Route to the compliance team.** Support does not adjudicate, override, or
   explain screening decisions.
4. Log the case with the vault, transaction, destination address, and screening
   result.
5. Communicate only the outcome and the process, never the reason.

**Customer message template**
> Hi [Name],
>
> This transfer was stopped by an automated compliance screening check before it
> reached the network. Your funds were not moved and remain fully available in
> your wallet.
>
> Screening decisions are reviewed by our compliance team rather than by support,
> so I've passed the details to them. They'll be in touch directly regarding next
> steps. I'm not able to share the specifics of what the screening flagged, and I
> appreciate that's frustrating — it's a constraint we're required to work under.

**Escalate when**
- Always route to compliance, never resolve in support.
- Escalate to engineering separately if screening is *timing out* or returning
  errors rather than decisions — that is a technical fault, not a compliance
  outcome.

## 4.2 Policy Limits Exceeded

**Symptoms**
- "It worked for a smaller amount"
- Larger transfers fail while routine ones succeed
- Fails at the same point each time

**Root cause**
Vault governance policies define what is permitted: per-transaction caps,
velocity limits, allowed destinations, approval thresholds that scale with
amount. This is the platform working exactly as configured — the customer's own
organisation set these rules, which is a helpful framing when the customer is
frustrated.

**Troubleshooting**
1. Review the vault's policy configuration against the attempted transfer.
2. Identify precisely which rule was triggered — amount, velocity, destination,
   asset, or time window.
3. Check whether the transfer instead requires *additional approvals* rather than
   being blocked outright — a different situation with a different answer.
4. If the policy needs changing, that is a vault-admin action on the customer's
   side, not a support change.

**Customer message template**
> Hi [Name],
>
> This transfer exceeded a limit set in your vault's own security policy — a
> [type] limit of [value]. Nothing failed technically; the platform stopped it as
> configured.
>
> Two options: split the transfer into amounts within the limit, or have a vault
> administrator at your organisation adjust the policy. Policy changes have to
> come from your admins rather than from us, which is deliberate — it's what
> stops anyone outside your organisation from loosening your controls.

**Escalate when**
- A transfer clearly within policy is blocked anyway.
- The policy engine is producing inconsistent results for identical requests.

## 4.3 Restricted Addresses and Address Book

**Symptoms**
- "It won't let me send to this address"
- New counterparties fail while existing ones work

**Root cause**
Vaults can be configured to allow transfers only to whitelisted Address Book
entries. New destinations must be added and approved before use. Separately, a
destination may appear on a sanctions or restricted list.

**Troubleshooting**
1. `ListAddressBookEntries` — is the destination present and approved?
2. Distinguish clearly between "not yet whitelisted" (a process step) and
   "restricted" (a compliance block, → §4.1). These sound similar to the customer
   and are completely different.
3. For whitelisting, walk the customer through adding the entry, including the
   approval it requires.

**Customer message template**
> Hi [Name],
>
> Your vault is configured to only allow transfers to approved addresses, and
> this destination hasn't been added yet. That's a safeguard rather than a fault —
> it's what prevents funds from being sent to an address someone slipped into an
> invoice.
>
> To add it: [steps], which then needs approval from [role]. Once approved,
> transfers to it will go through normally.

**Escalate when**
- A whitelisted, approved address is still rejected.
- The customer disputes a restriction — route to compliance, not engineering.

## 4.4 Geographic and Jurisdictional Restrictions

**Symptoms**
- Failures tied to a specific region or user
- Sudden failures for a previously working counterparty

**Root cause**
Regulatory obligations vary by jurisdiction and change over time. A counterparty
or corridor permitted last month may not be today.

**Troubleshooting**
1. Confirm the failure correlates with jurisdiction rather than a technical cause.
2. Route to compliance.
3. Do not speculate to the customer about regulations or their implications.

**Customer message template**
> Hi [Name],
>
> This transfer can't be completed due to regulatory restrictions applying to the
> jurisdiction involved. Your funds are unaffected and remain available.
>
> Our compliance team handles these determinations and can speak to what options
> exist. I've referred your case to them and they'll follow up directly.

**Escalate when**
- Always to compliance. Support does not interpret regulatory scope.

---

# §5 Technical Infrastructure Failures

Platform-side problems. The defining characteristic is that they affect
**multiple customers at once** — which is also how you recognise them.

## 5.1 Maintenance Windows

**Symptoms**
- Everything fails at once for a short, bounded period
- API returns `UNAVAILABLE`

**Troubleshooting**
1. Check the status page and internal maintenance calendar **before**
   investigating anything customer-specific.
2. Confirm the scope and expected end time.
3. Attach the ticket to the maintenance event rather than debugging it
   individually.

**Customer message template**
> Hi [Name],
>
> This is a scheduled maintenance window on our side, running until approximately
> [time] UTC. Requests during the window are rejected rather than processed
> incorrectly, so nothing is in an inconsistent state and no transactions were
> lost.
>
> Anything you attempted can be re-submitted once we're back. I'll confirm here
> when the window closes.

**Escalate when**
- The window overruns its stated end time.
- Behaviour after maintenance differs from before.

## 5.2 Node and Third-Party Outages

**Symptoms**
- One specific network fails while others work normally
- Balances stale on a single chain
- Broadcasts fail for one chain only

**Root cause**
The platform depends on blockchain node providers and external services (screening
providers, fee oracles). An outage at one provider degrades one specific
capability while everything else looks healthy — which is why "only Polygon is
broken" is a meaningful signal.

**Troubleshooting**
1. Establish scope: one chain or all? One operation type or all?
2. Check the relevant chain's own network status — the chain itself may be
   halted, which is not a platform fault.
3. Check whether it correlates with a known provider incident.
4. If more than one customer reports the same chain-specific symptom, open an
   incident. **Do not keep debugging it as individual tickets.**

**Customer message template**
> Hi [Name],
>
> We're seeing a service issue affecting [network] specifically — other networks
> are operating normally. Transactions on [network] are being queued rather than
> lost, and will process once service is restored.
>
> No funds are at risk. I'll update you as soon as I have a resolution time.

**Escalate when**
- Two or more customers report the same chain-specific failure. Immediately.
- Any inconsistency between reported balances and on-chain state.

## 5.3 Webhook Delivery Failures

**Symptoms**
- "Your system didn't notify us"
- Customer's records out of sync with the vault
- Missed transaction state changes

**Root cause**
Webhooks retry with exponential backoff for **up to 24 hours**, after which the
event is discarded permanently. If the customer's endpoint was down, rejecting
requests, returning a non-200 status, or failing signature verification, those
events are gone. The platform delivered them; the customer did not accept them.

**Troubleshooting**
1. Confirm the endpoint is reachable over HTTPS and returns 200.
2. Check whether signature verification is failing — a common cause is
   verifying against a re-serialised body instead of the **raw** request bytes.
   The signature is RSA-4096 / SHA-512 / PSS, base64, in the signature header.
3. Check whether the outage exceeded the 24-hour retry window.
4. For events already lost, reconcile via `ListTransactions` with a time filter —
   this is the recovery path, and every webhook integration should have one.
5. Recommend a periodic reconciliation job as a permanent backstop.

**Customer message template**
> Hi [Name],
>
> The events were sent — our system retried delivery for 24 hours, but your
> endpoint wasn't accepting them during that period, so they were eventually
> discarded.
>
> Nothing was lost on the transaction side: you can retrieve the full history
> through the transactions endpoint, and I can help you backfill the gap.
>
> Worth adding for the future: a periodic reconciliation check against the API
> catches anything webhooks miss, which is the standard pattern for exactly this
> situation.

**Escalate when**
- The endpoint is confirmed healthy and events still are not arriving.
- Signature verification fails against correctly captured raw payloads.

---

# Bonus

## Prevention — customer-facing recommendations

**Before any transfer**
- Use `"validateOnly": true` as a pre-flight check.
- Enable `"runSimulation": true` on anything material.
- Call `EstimateTransactionFee` and confirm the native balance covers it.
- Use Address Book entries instead of raw addresses. This is the single highest-value
  habit: it removes copy-paste and address-substitution risk from every future payment.

**Integration design**
- Generate `requestId` once per logical payment and reuse it across retries.
- Set `externalId` to your own reference so reconciliation is a lookup, not a hunt.
- Always set `expireTime` so unapproved transactions cannot linger and execute
  under changed conditions.
- Refresh JWTs well before expiry; never cache one at startup.
- Use webhooks instead of polling; add exponential backoff with jitter to retries.
- Run a scheduled reconciliation against `ListTransactions` regardless of webhook
  health.

**Operational hygiene**
- Keep the native coin funded in every wallet that sends tokens, or configure a
  Gas Station and monitor its balance.
- Maintain more than one authorised signer. A single designated signer is a
  single point of failure for every payment routed to them.
- Keep signer devices and CLI versions current.
- Document who approves what, so "waiting on approval" has a name attached.

## Monitoring — alerts worth having

| Alert | Condition | Why |
|---|---|---|
| Stuck pending | Transaction in a non-terminal state > 1 h | Catches the ticket before the customer opens it |
| Awaiting approval | Awaiting signature > 4 h | Surfaces signer unavailability early |
| Approaching expiry | `expireTime` < 2 h away, still unsigned | Last chance to act before it dies |
| Gas station low | Balance below N transactions' worth | Prevents fleet-wide gas failures |
| Native balance low | Any sending wallet below fee threshold | Same, per wallet |
| Auth error rate | Spike in `1001` / `1002` | Broken refresh logic or key rotation |
| Rate limit rate | Spike in `RESOURCE_EXHAUSTED` | Retry storm forming |
| Revert rate | `10002` above baseline | Contract or construction problem |
| Per-chain failure rate | One network's failures diverging from others | Node/provider outage signature |
| Webhook failures | Non-200 rate rising per customer endpoint | Warns before the 24-h window closes |
| Co-Signer heartbeat | No signing activity in expected window | Silent automation stoppage |
| Screening latency | AML result not returned in expected time | Provider degradation, not a compliance outcome |

The state-based alerts are best driven off `TRANSACTION_STATE_UPDATED` webhooks
plus a periodic `ListTransactions` sweep for transactions that have gone quiet —
the sweep is what catches the cases where the webhook itself never arrived.

## Reference links

**Platform documentation (internal)**
- API overview, authentication, and error codes
- Asset type reference and supported blockchains
- Transaction initiation and asset transfer guides
- Webhooks and automated co-signer setup


**External tools**
- Etherscan and equivalent per-chain explorers — transaction status, revert reasons
- Etherscan Gas Tracker — independent verification of network fee conditions
- gRPC status codes — `https://grpc.io/docs/guides/status-codes/`

---

## Agent quick reference

| Customer says | Most likely | Go to |
|---|---|---|
| "Pending for hours, visible on explorer" | Fee too low | §1.1 |
| "Pending, not on explorer at all" | Never signed — approval or policy | §2.2, §4 |
| "Failed but I was charged" | On-chain revert | §1.4 |
| "Says insufficient funds but I have plenty" | No native coin for gas | §2.1 |
| "Stopped working an hour after deploying" | Token expiry | §3.2 |
| "Charged twice" | requestId regenerated on retry | §3.4 |
| "Worked for a smaller amount" | Policy limit | §4.2 |
| "Won't let me send to this address" | Whitelist or screening | §4.3, §4.1 |
| "Only [one chain] is broken" | Provider outage | §5.2 |
| "You didn't notify us" | Webhook delivery | §5.3 |

**Three things to get right every time**
1. Establish where in the lifecycle it stopped before saying anything about cause.
2. Tell the customer plainly whether funds moved. This is what they actually want
   to know, and it is answerable in seconds.
3. If two or more customers report the same symptom, stop working tickets and
   raise an incident.
