Audit Report

## Title
Tx-Pool Eviction Timestamp Ordering Inverts Stated Intent, Enabling Targeted Eviction of Oldest Pending Transactions — (File: `tx-pool/src/component/sort_key.rs`)

## Summary

`EvictKey::cmp` uses ascending timestamp ordering (`self.timestamp.cmp(&other.timestamp)`), causing the **oldest** transaction to be selected for eviction first. The struct's own doc-comment states the opposite intent: "select the **latest** timestamp, for eviction." An attacker who submits minimum-fee-rate transactions **after** a victim's transaction can trigger `limit_size` to evict the victim's older transaction while the attacker's newer ones remain, at the ongoing cost of a single minimum-fee transaction per eviction cycle.

## Finding Description

**Root cause — `sort_key.rs` lines 92–103:**
`EvictKey::cmp` compares timestamps in ascending order. Because `iter_by_evict_key()` iterates ascending and `next_evict_entry` takes the first match, the entry with the **smallest** (oldest) timestamp is always selected for eviction when fee rates and descendant counts are equal. The doc-comment at lines 76–78 explicitly states the intent is to evict the **latest** timestamp.

**Eviction path:**
1. `process.rs:149–152` — after inserting each new transaction, `limit_size` is called immediately.
2. `pool.rs:298–304` — `limit_size` loops while `total_tx_size > max_tx_pool_size`, calling `next_evict_entry(Status::Pending)` each iteration.
3. `pool_map.rs:380–385` — `next_evict_entry` calls `iter_by_evict_key()` (ascending) and returns the first entry matching the requested status.
4. `sort_key.rs:96` — `self.timestamp.cmp(&other.timestamp)` places the oldest timestamp at the front of the ascending iterator, making it the eviction candidate.

**Existing checks are insufficient:** There is no guard that protects older transactions from eviction when fee rates are equal. The `descendants_count` tiebreaker only helps transactions with active descendants; isolated minimum-fee-rate transactions with no descendants are fully exposed.

**Confirmed by two unit tests:**
- `test_min_timestamp_evict` (`entry.rs:22–36`) sorts three equal-fee-rate `EvictKey` values and asserts the result is `[30, 31, 32]` — oldest first.
- `test_pool_evict` (`pending.rs:278–313`) inserts three equal-fee-rate entries with `thread::sleep` between them and asserts `tx1` (oldest) is always evicted first.

**Attack scenario:**
1. Alice submits `TX_A` at `min_fee_rate` → enters pool with timestamp `T_A`.
2. Bob fills remaining pool capacity with `min_fee_rate` transactions submitted after Alice (`T_B > T_A`).
3. Bob submits one additional transaction → `limit_size` is triggered → `TX_A` (oldest timestamp) is evicted; Bob's transactions remain.
4. Alice resubmits → Bob repeats step 3. Per-eviction cost: one minimum-fee transaction.

## Impact Explanation

This matches **High (10001–15000 points): "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** An unprivileged attacker reachable via `send_transaction` RPC or P2P relay can persistently prevent any specific minimum-fee-rate transaction from being confirmed. The one-time pool-fill cost is bounded (~1.8 CKB in fees for the 180 MB default pool), and the ongoing per-eviction cost is a single minimum-fee transaction. The victim must either continuously resubmit at a higher fee rate or accept indefinite delay, degrading the usability of the minimum-fee tier for all participants.

## Likelihood Explanation

No privileged access, key material, or majority hashpower is required. Any node reachable via RPC or P2P can execute the attack. The attack is most effective during low-activity periods when the pool is not already saturated with high-fee-rate transactions. The cost is predictable and repeatable. The victim has no in-protocol recourse other than raising their fee rate.

## Recommendation

Reverse the timestamp comparison in `EvictKey::cmp` to match the stated intent — evict the **newest** transaction first (descending order):

```rust
// tx-pool/src/component/sort_key.rs
if self.descendants_count == other.descendants_count {
    other.timestamp.cmp(&self.timestamp)  // descending: newest evicted first
} else {
    self.descendants_count.cmp(&other.descendants_count)
}
```

Update `test_min_timestamp_evict` to assert `vec![32, 31, 30]` and `test_pool_evict` to assert `tx3` (newest) is evicted first, confirming the fix.

## Proof of Concept

**Existing unit test directly confirms the bug** (`tx-pool/src/component/tests/entry.rs:22–36`):

```rust
result.sort();
assert_eq!(
    result.iter().map(|key| key.timestamp).collect::<Vec<_>>(),
    vec![30, 31, 32]  // oldest (30) is first = evicted first
);
```

The test name `test_min_timestamp_evict` and its assertion prove the oldest timestamp is evicted first, contradicting the doc-comment's stated intent of evicting the latest.

**Integration-level confirmation** (`tx-pool/src/component/tests/pending.rs:278–313`): `test_pool_evict` inserts three equal-fee-rate transactions with real `thread::sleep` delays and asserts `tx1` (inserted first, oldest timestamp) is always the first eviction candidate — directly modeling the attack precondition.