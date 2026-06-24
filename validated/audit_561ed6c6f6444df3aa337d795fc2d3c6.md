Audit Report

## Title
RBF Griefing: Attacker Self-Replacement Raises `min_replace_fee` to Deny Victim's Transaction Replacement - (File: `tx-pool/src/pool.rs`)

## Summary

`calculate_min_replace_fee` in `tx-pool/src/pool.rs` derives the replacement fee threshold entirely from the current live pool state. An attacker who controls a pending transaction can self-replace it with a fee equal to the victim's queued replacement fee, causing `min_replace_fee` to increase by one `extra_rbf_fee` increment before the victim's write lock is acquired. This can be repeated indefinitely at a cost of ~300 shannons per round, blocking the victim's replacement until the attacker's transaction is committed.

## Finding Description

`check_rbf` (called inside `with_tx_pool_write_lock` in `submit_entry`, `tx-pool/src/process.rs` L103–106) invokes `calculate_min_replace_fee` against the live pool state at the moment the write lock is held:

```rust
// tx-pool/src/pool.rs L101-114
fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
    let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
    ...
    sum.safe_add(extra_rbf_fee)
}
```

And the rejection check at L664–671:

```rust
let fee = entry.fee;
if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
    if fee < min_replace_fee {
        return Err(Reject::RBFRejected(...));
    }
}
```

The write lock comment at `process.rs` L104 ("check_rbf must be invoked in `write` lock to avoid concurrent issues") prevents two simultaneous `check_rbf` calls from racing each other, but it does **not** prevent the attacker from submitting a self-replacement that is enqueued and processed before the victim's transaction acquires the lock. Once the attacker's self-replacement (`tx_A'`) is committed to the pool under the write lock, the victim's subsequent write-lock acquisition sees the updated pool state where `min_replace_fee = fee(tx_A') + extra_rbf_fee > fee(tx_B)`, causing rejection.

The `min_replace_fee` RPC field exposed by `get_transaction_with_verbosity` gives the victim a snapshot that is immediately stale once the attacker self-replaces. There is no rate-limiting, cooldown, or frozen-threshold mechanism to prevent repeated self-replacement.

At the default `min_rbf_rate = 1500 shannons/KB` (`resource/ckb.toml` L214), for a ~200-byte transaction, `extra_rbf_fee = 1500 * 200 / 1000 = 300 shannons` per round.

## Impact Explanation

This matches the **High** allowed impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

An attacker can sustain the griefing for the entire 2–10 block proposal/commitment window at a total cost of a few thousand shannons (≈ 0.00003 CKB). During this window, the victim cannot evict the attacker's transaction from the pool, allowing the attacker's original (potentially malicious) transaction to be committed. Concrete consequences include: a competing transaction that would have replaced the attacker's being permanently blocked, and the victim wasting RPC round-trips and escalating fees without effect.

## Likelihood Explanation

- RBF is the documented default when `min_rbf_rate > min_fee_rate` (enabled by default at 1500 vs 1000 shannons/KB).
- The attacker only needs to poll `get_transaction_with_verbosity` to observe the victim's pending replacement fee, then submit a self-replacement via `send_transaction` RPC — no special privilege required.
- The race condition is favorable to the attacker: the attacker can submit immediately upon observing the victim's transaction in the mempool, while the victim's transaction is still in the async verify queue.
- Cost per round is ~300 shannons; sustaining for 10 blocks costs ~3000 shannons total.

## Recommendation

1. **Freeze the replacement threshold at first conflict entry**: Record `min_replace_fee` when the conflicting transaction first enters the pool and use that frozen value for all subsequent replacement checks against the same input set. This prevents self-replacement from raising the bar.
2. **Rate-limit self-replacements**: Enforce a minimum time gap (e.g., one block interval) between successive replacements of the same input set, reducing the attacker's ability to frontrun within a single block.
3. **Use the original conflicting tx's fee, not the current pool state**: When computing `min_replace_fee`, use the fee of the transaction that was in the pool at the time the victim's replacement was first submitted, not the fee of whatever transaction currently occupies that input.

## Proof of Concept

1. Attacker submits `tx_A` spending input `I` with fee `F = 100_000_000` shannons.
2. Victim calls `get_transaction_with_verbosity(tx_A, 2)` → `min_replace_fee = F + 300 = 100_000_300`.
3. Victim constructs `tx_B` spending `I` with fee `V = 100_000_300` and submits via RPC.
4. Before `tx_B` acquires the write lock, attacker submits `tx_A'` spending `I` with fee `F' = V = 100_000_300` (valid self-replacement: `F' >= F + extra_rbf_fee`).
5. `tx_A'` acquires the write lock first; `tx_A` is evicted, `tx_A'` enters the pool.
6. `tx_B` acquires the write lock next; `check_rbf` computes `min_replace_fee = F' + 300 = 100_000_600 > V` → `RBFRejected`.
7. Victim must now submit `tx_C` with fee ≥ 100_000_600. Attacker repeats step 4 with fee 100_000_600.
8. Each round costs the attacker 300 shannons. After `tx_A'` enters the proposal window (2–10 blocks), the attacker sustains this until commitment at a total cost of ~3000 shannons.

Reproducible as an integration test by extending `test/src/specs/tx_pool/replace.rs`: submit two concurrent RPC calls where the attacker's self-replacement is enqueued before the victim's replacement, then assert that the victim receives `RBFRejected` despite offering the correct fee.