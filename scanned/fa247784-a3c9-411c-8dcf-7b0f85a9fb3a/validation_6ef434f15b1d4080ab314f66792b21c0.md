### Title
RBF Griefing Attack — Attacker Can Repeatedly Raise `min_replace_fee` to Deny Transaction Replacement - (File: `tx-pool/src/pool.rs`)

---

### Summary

When RBF is enabled, an attacker who controls a pending transaction can repeatedly self-replace it with a fee equal to the victim's queued replacement fee. Because `min_replace_fee` is recomputed live from the pool state, each self-replacement raises the bar by exactly one `extra_rbf_fee` increment, causing the victim's replacement to fail indefinitely. The attacker can sustain this until their original transaction is proposed and committed, at a cost of only ~300 shannons per round.

---

### Finding Description

`check_rbf` in `tx-pool/src/pool.rs` enforces:

```rust
// Rule #4 / #3
let fee = entry.fee;
if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
    if fee < min_replace_fee {
        return Err(Reject::RBFRejected(format!(
            "Tx's current fee is {}, expect it to >= {} to replace old txs",
            fee, min_replace_fee,
        )));
    }
}
```

`calculate_min_replace_fee` computes:

```rust
// min_replace_fee = sum(replaced_txs.fee) + extra_rbf_fee
fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
    let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
    ...
    sum.safe_add(extra_rbf_fee)
}
```

The `min_replace_fee` is derived entirely from the **current live pool state** of the conflicting transaction. Because `check_rbf` is called inside the write lock in `submit_entry`, the pool state it reads is always the most recent committed state — meaning any self-replacement the attacker submitted just before the victim's write-lock acquisition will already be reflected.

The attacker's self-replacement is itself subject to the same RBF rules, but the minimum increment is only `extra_rbf_fee` (≈ 300 shannons at `min_rbf_rate = 1500 shannons/KB` for a ~200-byte tx). This is negligible.

The `min_replace_fee` RPC field exposed by `get_transaction_with_verbosity` gives the victim a snapshot that is immediately stale once the attacker self-replaces.

---

### Impact Explanation

**Scenario — attacker delays replacement until commitment:**

CKB's two-phase proposal/commitment window means a proposed transaction can be committed 2–10 blocks after proposal. Once the attacker's tx is included in a proposal, the attacker only needs to sustain the griefing for those few blocks. During that window the victim cannot successfully replace the attacker's tx, so the attacker's original (potentially malicious) transaction is committed.

Concrete impact:
- A double-spend transaction submitted by the attacker cannot be evicted from the pool by the victim during the proposal window.
- A low-fee transaction that would otherwise be replaced by a higher-fee competing transaction is kept alive.
- The victim wastes RPC round-trips and must continuously escalate their fee, while the attacker pays only ~300 shannons per frontrun.

---

### Likelihood Explanation

- RBF is an opt-in node configuration (`min_rbf_rate > min_fee_rate`), but it is the documented default for nodes that want fee-based replacement.
- The attacker only needs to watch the public mempool (or poll `get_transaction_with_verbosity`) to observe the victim's pending replacement fee, then submit a self-replacement with that exact fee before the victim's write lock is acquired.
- The cost per round is ~300 shannons (≈ 0.000003 CKB), making sustained griefing economically trivial.
- No special privilege is required — any unprivileged `send_transaction` RPC caller can execute this.

---

### Recommendation

1. **Clamp the replacement fee check**: When `fee < min_replace_fee`, instead of rejecting outright, accept the replacement at the victim's offered fee if it exceeds the *original* (pre-self-replacement) conflicting tx's fee plus `extra_rbf_fee`. This mirrors the external report's mitigation of clamping `_amount` to the available collateral.

2. **Snapshot `min_replace_fee` at first conflict detection**: Record the `min_replace_fee` threshold at the moment the conflicting tx first enters the pool, and use that frozen value for all subsequent replacement checks against that input set. This prevents the attacker from raising the bar via self-replacement.

3. **Rate-limit self-replacements**: Enforce a minimum time gap between successive replacements of the same input set, reducing the attacker's ability to frontrun within a single block interval.

---

### Proof of Concept

1. Attacker submits `tx_A` with fee `F` (e.g., 100,000,000 shannons) spending input `I`.
2. Victim calls `get_transaction_with_verbosity(tx_A, 2)` → `min_replace_fee = F + extra_rbf_fee` (e.g., 100,000,363).
3. Victim constructs `tx_B` spending `I` with fee `V = 100,000,363` and submits it.
4. Attacker observes `tx_B` in the mempool, immediately submits `tx_A'` spending `I` with fee `F' = V = 100,000,363` (satisfies `F' >= F + extra_rbf_fee`).
5. `tx_A'` acquires the write lock first; `tx_A` is evicted and replaced by `tx_A'`.
6. `tx_B` acquires the write lock next; `check_rbf` computes `min_replace_fee = F' + extra_rbf_fee = 100,000,726 > V = 100,000,363` → `RBFRejected`.
7. Victim must now submit `tx_C` with fee ≥ 100,000,726. Attacker repeats step 4 with fee 100,000,726.
8. Each round costs the attacker 363 shannons. After `tx_A'` is included in a block proposal, the attacker sustains this for 2–10 blocks until commitment, at a total cost of a few thousand shannons.

Root cause lines: [1](#0-0) [2](#0-1) [3](#0-2)

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
