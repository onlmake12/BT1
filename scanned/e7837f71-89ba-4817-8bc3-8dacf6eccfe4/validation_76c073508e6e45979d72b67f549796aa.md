### Title
RBF `calculate_min_replace_fee` Uses Only Byte-Size, Ignoring Cycles Dimension of Transaction Weight — (`File: tx-pool/src/pool.rs`)

### Summary

`calculate_min_replace_fee` computes the mandatory extra RBF fee using only the replacement transaction's serialized byte-size, while the canonical transaction weight is `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For cycle-dominated replacement transactions the extra fee threshold is severely underestimated, allowing an RPC caller to replace pool transactions at a fraction of the cost the node operator's `min_rbf_rate` configuration intends.

### Finding Description

CKB defines transaction weight as the larger of two resource dimensions:

```rust
// util/types/src/core/tx_pool.rs:298-303
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

`TxEntry::fee_rate()` correctly uses both dimensions:

```rust
// tx-pool/src/component/entry.rs:115-118
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

However, `calculate_min_replace_fee` — called from `check_rbf` — computes the mandatory extra fee using **only** `size`:

```rust
// tx-pool/src/pool.rs:102-103
fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
    let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
```

`check_rbf` passes `entry.size` (bytes only):

```rust
// tx-pool/src/pool.rs:665
if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
```

The `cycles` field of the incoming `TxEntry` is never consulted. This is the direct analog of the reported `getAmountOut` bug: one parameter (`liquidityDelta` / `size`) is used to adjust one dimension of the calculation while the correlated dimension (`virtualX`/`virtualY` / `cycles`) is silently ignored, producing an incorrect result.

`check_tx_fee` applies the same size-only shortcut but explicitly documents it as an intentional cheap pre-filter:

```rust
// tx-pool/src/util.rs:42-45
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

No equivalent comment or design rationale exists for the RBF path, and the RBF check is the **binding** gate that determines whether a replacement is accepted — it is not a cheap pre-filter.

### Impact Explanation

With `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` and `max_tx_verify_cycles = 70_000_000`, a compact replacement transaction (e.g., 200 bytes) that consumes the maximum allowed cycles has an effective weight of:

```
weight = max(200, 70_000_000 × 0.000_170_571_4) ≈ max(200, 11_940) = 11_940
```

The extra RBF fee is computed on `size = 200` instead of `weight = 11_940`, underestimating it by ~60×. An attacker can therefore replace a legitimate pool transaction while paying ~60× less extra fee than `min_rbf_rate` requires. At the default `min_rbf_rate = 1500 shannons/KB`, the correct extra fee would be ~17,910 shannons but only ~300 shannons are required. This:

1. Lets an RPC caller bypass the node operator's `min_rbf_rate` economic guard for high-cycles replacements.
2. Enables cheap, repeated replacement of pool transactions with computationally expensive ones, increasing validation cost on the node without proportional fee payment.
3. Degrades the economic incentive that RBF is designed to enforce.

### Likelihood Explanation

RBF is opt-in per node (`min_rbf_rate > min_fee_rate`), but the default configuration in `ckb.toml` ships with RBF enabled (`min_fee_rate = 1_000`, `min_rbf_rate = 1_500`). Any unprivileged RPC caller with access to the `send_transaction` endpoint — the standard tx-pool submission path — can trigger this path. No special privilege, key, or majority hashpower is required. The attacker only needs to know the pool contains a replaceable transaction and craft a high-cycles replacement.

### Recommendation

Replace the size-only argument with the full transaction weight in `check_rbf` and `calculate_min_replace_fee`:

```rust
// check_rbf call site (tx-pool/src/pool.rs:665)
let weight = get_transaction_weight(entry.size, entry.cycles);
if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, weight) {
```

```rust
// calculate_min_replace_fee signature (tx-pool/src/pool.rs:102)
fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], weight: usize) -> Option<Capacity> {
    let extra_rbf_fee = self.config.min_rbf_rate.fee(weight as u64);
```

This mirrors how `TxEntry::fee_rate()` already correctly computes the effective fee rate using both dimensions.

### Proof of Concept

1. Enable RBF on a node (`min_rbf_rate = 1500`, `min_fee_rate = 1000`).
2. Submit a baseline transaction `tx1` spending a live cell with fee = F shannons and size = S bytes.
3. Craft a replacement `tx2` spending the same input with:
   - `size` ≈ 200 bytes (small serialized size)
   - `cycles` = 70,000,000 (near `max_tx_verify_cycles`)
   - `fee` = F + 300 shannons (only 300 extra, far below the correct threshold of ~17,910 extra shannons)
4. Submit `tx2` via `send_transaction` RPC.
5. Observe that `check_rbf` accepts `tx2` because `calculate_min_replace_fee` computes `extra_rbf_fee = 1500 × 200 / 1000 = 300` shannons instead of the correct `1500 × 11940 / 1000 = 17910` shannons.

The root cause is at: [1](#0-0) 

Called with size-only from: [2](#0-1) 

Contrast with the correct weight calculation used in `fee_rate()`: [3](#0-2) 

And the canonical weight definition: [4](#0-3)

### Citations

**File:** tx-pool/src/pool.rs (L101-103)
```rust
    /// min_replace_fee = sum(replaced_txs.fee) + extra_rbf_fee
    fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
        let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
```

**File:** tx-pool/src/pool.rs (L662-665)
```rust
        // Rule #4, new tx's fee need to higher than min_rbf_fee computed from the tx_pool configuration
        // Rule #3, new tx's fee need to higher than conflicts, here we only check the all conflicted txs fee
        let fee = entry.fee;
        if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```
