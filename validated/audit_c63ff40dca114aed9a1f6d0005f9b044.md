All cited code references are confirmed in the repository. Let me verify the final piece — whether a stripped-witness transaction passes `non_contextual_verify`:

Audit Report

## Title
Witness-Stripping Front-Run Enables Persistent Transaction DoS via `proposal_short_id` Deduplication — (`File: tx-pool/src/util.rs`)

## Summary
The tx-pool and verify queue deduplicate transactions using `proposal_short_id`, which is derived solely from `tx_hash` over `RawTransaction` (excluding witnesses). An attacker who observes a pending transaction in the P2P relay stream can strip its witnesses and submit the stripped version first, causing the legitimate transaction to be permanently rejected as `PoolRejectedDuplicatedTransaction`. The attack is zero-cost and indefinitely repeatable.

## Finding Description
`check_txid_collision` in `tx-pool/src/util.rs` (L20–26) deduplicates solely by `proposal_short_id`:

```rust
pub(crate) fn check_txid_collision(tx_pool: &TxPool, tx: &TransactionView) -> Result<(), Reject> {
    let short_id = tx.proposal_short_id();
    if tx_pool.contains_proposal_id(&short_id) {
        return Err(Reject::Duplicated(tx.hash()));
    }
    Ok(())
}
```

`tx_hash` is computed over `RawTransaction` only, confirmed at `util/gen-types/src/extension/calc_hash.rs` L140–142 (`self.raw().calc_tx_hash()`), explicitly excluding witnesses. Two transactions with identical inputs/outputs but different witnesses share the same `tx_hash` and `proposal_short_id`.

The verify queue (`tx-pool/src/component/verify_queue.rs` L37–38) uses `ProposalShortId` as its unique index key (`#[multi_index(hashed_unique)] id: ProposalShortId`). `resumeble_process_tx` in `tx-pool/src/process.rs` (L349–351) checks `verify_queue_contains` before enqueuing — the block occurs even before the tx reaches the pool.

`NonContextualTransactionVerifier` (confirmed at `verification/src/transaction_verifier.rs` L94–102) checks only version, size, empty inputs/outputs, duplicate deps, outputs data, and script hash type — **no witness count or content check**. A stripped-witness transaction passes non-contextual verification and enters the verify queue normally.

The comment at `process.rs` L280 — *"Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc."* — is factually incorrect and masks the design flaw.

Exploit path:
1. Attacker observes `tx_user` (with valid secp256k1 witness) in the P2P relay stream.
2. Attacker constructs `tx_attacker` by stripping all witnesses. `tx_attacker.hash() == tx_user.hash()`, `tx_attacker.proposal_short_id() == tx_user.proposal_short_id()`, but `tx_attacker.witness_hash() != tx_user.witness_hash()`.
3. Attacker submits `tx_attacker` first — passes non-contextual checks and enters the verify queue.
4. User submits `tx_user` — `verify_queue_contains` returns `true` (same `proposal_short_id`) → `Err(Reject::Duplicated(...))`.
5. `tx_attacker` fails script verification (secp256k1 rejects empty witness) and is evicted.
6. Attacker immediately repeats from step 2. `tx_user` is permanently blocked.

The existing integration test at `test/src/specs/tx_pool/collision.rs` (L214–223) explicitly confirms the mechanism: `tx2 = tx1.as_advanced_builder().witness(Bytes::default()).build()` produces `tx1.hash() == tx2.hash()` and `tx1.witness_hash() != tx2.witness_hash()`. The test at L16–37 confirms that submitting `tx2` after `tx1` yields `PoolRejectedDuplicatedTransaction`. The attack simply inverts the submission order.

## Impact Explanation
**High** — matches *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."* An attacker with P2P relay access can block any specific transaction from ever being confirmed on any targeted node, with zero fee cost (rejected transactions pay no fees). For time-sensitive transactions (DAO withdrawals, `since`-locked cells near expiry), this can cause permanent loss of a time window. Applied at scale across many nodes simultaneously, it degrades network-wide transaction throughput.

## Likelihood Explanation
Any P2P relay peer connected to the victim node observes relayed transactions before pool admission — no special privileges required. The attacker needs only to submit to the same node faster than the legitimate user, trivially achievable for a co-located or well-connected peer. The attack requires no key material, no hashpower, and no victim mistakes. Cost per attack iteration is zero (no fee for rejected transactions), making indefinite repetition practical.

## Recommendation
1. `check_txid_collision` should additionally check `witness_hash`: if a transaction with the same `tx_hash` but a different `witness_hash` is already in the pool, apply a replacement policy (prefer the version with a non-empty witness, or require a fee bump) rather than blindly rejecting the newcomer.
2. The verify queue's `VerifyEntry` unique index should use `witness_hash` (or a composite of `tx_hash` + `witness_hash`) as the deduplication key, so a stripped-witness version does not block the legitimate version from entering the queue.
3. Correct the misleading comment at `process.rs` L280 that incorrectly claims `tx_hash` covers witnesses.

## Proof of Concept
The existing test at `test/src/specs/tx_pool/collision.rs` L16–37 already demonstrates the deduplication behavior. To reproduce the attack scenario:

1. Run a CKB node with a secp256k1-locked cell.
2. Construct `tx_user` spending that cell with a valid signature witness.
3. Construct `tx_attacker = tx_user` with `witnesses = []` (via `as_advanced_builder().witness(Bytes::default()).build()`).
4. Verify `tx_attacker.hash() == tx_user.hash()` and `tx_attacker.witness_hash() != tx_user.witness_hash()`.
5. Submit `tx_attacker` via RPC before `tx_user` — observe it enters the verify queue.
6. Submit `tx_user` — observe `PoolRejectedDuplicatedTransaction`.
7. Wait for `tx_attacker` to be evicted (script verification failure due to empty witness).
8. Repeat steps 5–7 indefinitely — `tx_user` is never admitted.