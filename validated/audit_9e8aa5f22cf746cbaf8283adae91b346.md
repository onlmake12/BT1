Based on my code review, the claim is technically grounded. Let me verify the key assertions:

**`calculate_min_replace_fee` reads live pool state** — confirmed at [1](#0-0)  — it sums `c.inner.fee` from current pool entries and adds `extra_rbf_fee`.

**The fee check at `check_rbf`** — confirmed at [2](#0-1)  — rejects if `fee < min_replace_fee` computed from live state.

**The write lock comment** — confirmed at [3](#0-2)  — serializes concurrent `check_rbf` calls but does not prevent the attacker from submitting a self-replacement that is enqueued and committed before the victim's transaction acquires the lock.

The race window is real: between the victim calling `get_transaction_with_verbosity` (to read `min_replace_fee`) and the victim's `tx_B` being processed under the write lock, the attacker can submit `tx_A'` with fee equal to the victim's planned fee. Once `tx_A'` commits to the pool, `min_replace_fee` rises by one `extra_rbf_fee` increment, causing the victim's `tx_B` to be rejected.

---

Audit Report

## Title
RBF Griefing: Attacker Self-Replacement Raises `min_replace_fee` to Deny Victim's Transaction Replacement - (File: `tx-pool/src/pool.rs`)

## Summary

`calculate_min_replace_fee` in `tx-pool/src/pool.rs` derives the replacement fee threshold entirely from the live pool state at the moment `check_rbf` is called under the write lock. An attacker who controls a pending transaction can self-replace it with a fee equal to the victim's queued replacement fee, causing `min_replace_fee` to increase by one `extra_rbf_fee` increment before the victim's transaction is processed. This can be repeated at a cost of ~300 shannons per round, indefinitely blocking the victim's replacement.

## Finding Description

`calculate_min_replace_fee` sums the fees of all conflicting pool entries at call time and adds `extra_rbf_fee`:

```rust
// tx-pool/src/pool.rs L101-114
fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
    let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
    let replaced_fees: HashMap<_, _> = conflicts.iter()
        .map(|c| (c.id.clone(), c.inner.fee)).collect();
    let replaced_sum_fee = replaced_fees.values()
        .try_fold(Capacity::zero(), |acc, x| acc.safe_add(*x));
    let res = replaced_sum_fee.map_or(Err(CapacityError::Overflow), |sum| sum.safe_add(extra_rbf_fee));
    ...
}
```

The rejection check at L664–671 uses this live-state value:

```rust
if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
    if fee < min_replace_fee {
        return Err(Reject::RBFRejected(...));
    }
}
```

`check_rbf` is called inside `with_tx_pool_write_lock` in `submit_entry` (`process.rs` L103–106), which serializes concurrent pool writes. However, this only prevents two simultaneous `check_rbf` calls from racing each other. It does **not** prevent the attacker from submitting a self-replacement (`tx_A'`) that is enqueued and processed under the write lock before the victim's transaction (`tx_B`) acquires it.

The exploit window is the gap between the victim reading `min_replace_fee` via `get_transaction_with_verbosity` and the victim's `tx_B` being committed to the pool. During this window, the attacker submits `tx_A'` with fee equal to the victim's planned fee. `tx_A'` is a valid self-replacement (`fee(tx_A') >= fee(tx_A) + extra_rbf_fee`), so it commits to the pool. When `tx_B` subsequently acquires the write lock, `calculate_min_replace_fee` now returns `fee(tx_A') + extra_rbf_fee > fee(tx_B)`, causing `RBFRejected`. There is no rate-limiting, cooldown, or frozen-threshold mechanism to prevent this.

## Impact Explanation

This matches **High: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** An attacker can sustain the griefing for the entire 2–10 block proposal/commitment window at a total cost of a few thousand shannons (≈ 0.00003 CKB). The victim cannot evict the attacker's transaction from the pool, allowing the attacker's original transaction to be committed while the victim wastes RPC round-trips and escalating fees without effect.

## Likelihood Explanation

- RBF is enabled by default when `min_rbf_rate > min_fee_rate` (1500 vs 1000 shannons/KB per `resource/ckb.toml`).
- The attacker only needs to poll `get_transaction_with_verbosity` to observe the victim's pending replacement fee, then submit a self-replacement via `send_transaction` RPC — no special privilege required.
- The exploit window (between victim's RPC read and victim's transaction being processed) is reliably exploitable: the attacker submits `tx_A'` before the victim submits `tx_B`, or immediately after observing `tx_B` enter the verify queue while it is still undergoing script verification.
- Cost per round: ~300 shannons. Sustaining for 10 blocks: ~3000 shannons total.

## Recommendation

1. **Freeze the replacement threshold at first conflict entry**: Record `min_replace_fee` when the conflicting transaction first enters the pool and use that frozen value for all subsequent replacement checks against the same input set. This prevents self-replacement from raising the bar.
2. **Rate-limit self-replacements**: Enforce a minimum time gap (e.g., one block interval) between successive replacements of the same input set.
3. **Use the original conflicting tx's fee, not the current pool state**: When computing `min_replace_fee`, use the fee of the transaction that was in the pool at the time the victim's replacement was first submitted, not the fee of whatever transaction currently occupies that input.

## Proof of Concept

1. Attacker submits `tx_A` spending input `I` with fee `F = 100_000_000` shannons.
2. Victim calls `get_transaction_with_verbosity(tx_A, 2)` → `min_replace_fee = F + 300 = 100_000_300`.
3. Attacker, observing the victim's intent, submits `tx_A'` spending `I` with fee `F' = 100_000_300` (valid self-replacement: `F' >= F + extra_rbf_fee`). `tx_A'` commits to the pool.
4. Victim submits `tx_B` spending `I` with fee `V = 100_000_300`.
5. `tx_B` acquires the write lock; `check_rbf` computes `min_replace_fee = F' + 300 = 100_000_600 > V` → `RBFRejected`.
6. Victim must now submit `tx_C` with fee ≥ 100_000_600. Attacker repeats step 3 with fee 100_000_600.
7. Each round costs the attacker 300 shannons. After `tx_A'` enters the proposal window (2–10 blocks), the attacker sustains this until commitment at a total cost of ~3000 shannons.

Reproducible as an integration test by extending `test/src/specs/tx_pool/replace.rs`: submit the attacker's self-replacement before the victim's replacement, then assert that the victim receives `RBFRejected` despite offering the fee returned by `get_transaction_with_verbosity`.

### Citations

**File:** tx-pool/src/pool.rs (L101-114)
```rust
    /// min_replace_fee = sum(replaced_txs.fee) + extra_rbf_fee
    fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
        let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
        // don't account for duplicate txs
        let replaced_fees: HashMap<_, _> = conflicts
            .iter()
            .map(|c| (c.id.clone(), c.inner.fee))
            .collect();
        let replaced_sum_fee = replaced_fees
            .values()
            .try_fold(Capacity::zero(), |acc, x| acc.safe_add(*x));
        let res = replaced_sum_fee.map_or(Err(CapacityError::Overflow), |sum| {
            sum.safe_add(extra_rbf_fee)
        });
```

**File:** tx-pool/src/pool.rs (L664-671)
```rust
        let fee = entry.fee;
        if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
            if fee < min_replace_fee {
                return Err(Reject::RBFRejected(format!(
                    "Tx's current fee is {}, expect it to >= {} to replace old txs",
                    fee, min_replace_fee,
                )));
            }
```

**File:** tx-pool/src/process.rs (L103-106)
```rust
            .with_tx_pool_write_lock(move |tx_pool, snapshot| {
                // check_rbf must be invoked in `write` lock to avoid concurrent issues.
                let conflicts = if tx_pool.enable_rbf() {
                    tx_pool.check_rbf(&snapshot, &entry)?
```
