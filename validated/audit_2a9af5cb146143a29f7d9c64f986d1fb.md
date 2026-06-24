All cited code references are confirmed in the actual repository. The finding is valid.

Audit Report

## Title
`calculate_min_replace_fee` Uses Raw `size` Instead of `get_transaction_weight(size, cycles)` for RBF Fee Enforcement — (`tx-pool/src/pool.rs`)

## Summary
`calculate_min_replace_fee` computes the minimum required fee for an RBF replacement using only the transaction's serialized `size`, while every other fee-rate-sensitive path in the tx-pool (sorting, eviction, fee-rate reporting) uses `get_transaction_weight(size, cycles)`. For high-cycles transactions where `cycles * DEFAULT_BYTES_PER_CYCLES > size`, this underestimates the required fee increment, allowing an unprivileged submitter to replace pool transactions with a fee bump far below what the node's `min_rbf_rate` policy intends.

## Finding Description
`get_transaction_weight` is defined at [1](#0-0)  and returns `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. It is used consistently in:

- `fee_rate()`: [2](#0-1) 
- `AncestorsScoreSortKey`: [3](#0-2) 
- `EvictKey`: [4](#0-3) 

However, `calculate_min_replace_fee` diverges — it passes only raw `size` to `min_rbf_rate.fee()`: [5](#0-4) 

The caller `check_rbf` passes `entry.size` with no cycles argument: [6](#0-5) 

By contrast, `check_tx_fee` in `tx-pool/src/util.rs` also uses raw `size`, but carries an explicit comment acknowledging this as an intentional "cheap check": [7](#0-6) 

No such acknowledgment exists in `calculate_min_replace_fee`, and the RBF check is not a "cheap check" — it is the definitive enforcement gate for RBF admission. The inconsistency means a transaction can pass the RBF fee check using a size-only metric, yet be sorted and evicted using the weight metric, which is a larger value for high-cycles transactions.

`FeeRate::fee` computes `fee_rate * weight / 1000`: [8](#0-7) 

For a transaction with `size = 100`, `cycles = 10,000,000`, and `min_rbf_rate = 1000 shannons/KW`:
- `get_transaction_weight(100, 10_000_000)` ≈ 1705
- Expected extra fee: 1705 shannons
- Actual extra fee enforced: 100 shannons (~17× underestimate)

## Impact Explanation
An attacker can craft a replacement transaction with small serialized size but high cycle consumption, paying only the size-based extra fee increment. This bypasses the node operator's `min_rbf_rate` policy for the RBF fee bump, enabling repeated cheap replacements of pool transactions at a fraction of the intended cost. This constitutes a **bad design that could cause CKB network congestion with few costs** (High, 10001–15000 points): the RBF anti-spam mechanism is undermined for any high-cycles transaction, and the attacker's cost per replacement is proportionally reduced by the ratio `weight / size`.

## Likelihood Explanation
RBF must be enabled (`min_rbf_rate > min_fee_rate`), which is a supported and documented node configuration. Any unprivileged RPC caller can submit transactions. CKB scripts can be written to consume many cycles with minimal witness/output data (e.g., a tight loop in a lock script). No special privileges, leaked keys, or victim mistakes are required. The attack is repeatable as long as RBF is enabled.

## Recommendation
Replace the raw `size` argument in `calculate_min_replace_fee` with `get_transaction_weight(size, cycles)`. The caller already has access to `entry.cycles`:

```rust
// In check_rbf (tx-pool/src/pool.rs, L665):
self.calculate_min_replace_fee(&all_conflicted, entry.size, entry.cycles)

// In calculate_min_replace_fee (tx-pool/src/pool.rs, L102):
fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize, cycles: Cycle) -> Option<Capacity> {
    let weight = get_transaction_weight(size, cycles);
    let extra_rbf_fee = self.config.min_rbf_rate.fee(weight);
    ...
}
```

This aligns RBF fee enforcement with the weight metric used in sorting, eviction, and fee-rate reporting.

## Proof of Concept
1. Configure a CKB node with `min_rbf_rate > min_fee_rate`.
2. Submit victim transaction `T1` with moderate fee `F1` and moderate cycles.
3. Craft replacement `T2` spending the same inputs as `T1`, with:
   - Small serialized size (e.g., 100 bytes)
   - High cycle consumption (e.g., 10,000,000 cycles via a tight script loop)
   - Fee = `F1 + min_rbf_rate.fee(100)` (size-only increment, ~17× below weight-based increment)
4. Submit `T2` via `send_transaction` RPC.
5. Observe `T2` is accepted: `calculate_min_replace_fee` passes because it checks only `size`, even though `T2`'s effective fee rate computed via `get_transaction_weight` is far below `min_rbf_rate`.
6. Repeat with fresh victim transactions to demonstrate low-cost repeated replacement.

### Citations

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** tx-pool/src/component/entry.rs (L115-117)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
```

**File:** tx-pool/src/component/entry.rs (L221-231)
```rust
impl From<&TxEntry> for AncestorsScoreSortKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let ancestors_weight = get_transaction_weight(entry.ancestors_size, entry.ancestors_cycles);
        AncestorsScoreSortKey {
            fee: entry.fee,
            weight,
            ancestors_fee: entry.ancestors_fee,
            ancestors_weight,
        }
    }
```

**File:** tx-pool/src/component/entry.rs (L234-247)
```rust
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);

        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),
            timestamp: entry.timestamp,
            descendants_count: entry.descendants_count,
        }
    }
```

**File:** tx-pool/src/pool.rs (L102-103)
```rust
    fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
        let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
```

**File:** tx-pool/src/pool.rs (L665-665)
```rust
        if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
```

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

**File:** util/types/src/core/fee_rate.rs (L34-36)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
```
