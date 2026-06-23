### Title
Inconsistent Use of `get_transaction_weight` in RBF Fee Enforcement — (`tx-pool/src/pool.rs`)

---

### Summary

`get_transaction_weight(size, cycles)` is the canonical function for computing a transaction's effective weight in CKB's tx-pool. It is used consistently for sorting, eviction, and fee-rate estimation. However, `calculate_min_replace_fee` — which enforces the minimum fee increment for Replace-By-Fee (RBF) — uses raw `entry.size` directly instead of `get_transaction_weight(size, cycles)`. For high-cycles transactions, this underestimates the required fee bump, allowing any tx-pool submitter to replace existing pool transactions with a fee increment far below what the node operator's `min_rbf_rate` policy intends.

---

### Finding Description

`get_transaction_weight` is defined as:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [1](#0-0) 

It is used correctly in all fee-rate-sensitive paths:

- **Sorting** (`AncestorsScoreSortKey`): `get_transaction_weight(entry.size, entry.cycles)` [2](#0-1) 
- **Eviction** (`EvictKey`): `get_transaction_weight(entry.size, entry.cycles)` [3](#0-2) 
- **Fee-rate reporting** (`fee_rate()`): `get_transaction_weight(self.size, self.cycles)` [4](#0-3) 
- **Fee estimators**: both `confirmation_fraction` and `weight_units_flow` call `get_transaction_weight` [5](#0-4) 

However, `calculate_min_replace_fee` uses raw `size` directly:

```rust
fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
    let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
    ...
}
``` [6](#0-5) 

This is called from `check_rbf` with `entry.size` only — cycles are never passed:

```rust
if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
``` [7](#0-6) 

Note: `check_tx_fee` also uses `tx_size` directly, but it carries an explicit comment acknowledging this as an intentional "cheap check":

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [8](#0-7) 

No such acknowledgment exists in `calculate_min_replace_fee`. The two code paths diverge: sorting and eviction use `get_transaction_weight(size, cycles)`, while the RBF fee enforcement uses only `size`.

---

### Impact Explanation

`FeeRate::fee(weight)` computes `fee_rate * weight / 1000`. [9](#0-8) 

For a transaction where `cycles * DEFAULT_BYTES_PER_CYCLES > size`, `get_transaction_weight` returns a value much larger than `size`. The extra RBF fee required is therefore underestimated.

**Concrete example** with `min_rbf_rate = 1000 shannons/KW`:
- New tx: `size = 100 bytes`, `cycles = 10,000,000`
- `get_transaction_weight(100, 10_000_000) = max(100, 10_000_000 × 0.000_170_571_4) = 1705`
- **Expected** extra fee: `1000 × 1705 / 1000 = 1705 shannons`
- **Actual** extra fee required: `1000 × 100 / 1000 = 100 shannons`

The attacker pays ~17× less than the node operator's `min_rbf_rate` policy intends. The attacker can replace existing pool transactions with a fee increment far below the configured threshold, undermining the RBF anti-spam and fee-bumping guarantees.

---

### Likelihood Explanation

RBF must be enabled (`min_rbf_rate > min_fee_rate`), which is a supported and documented node configuration. Any unprivileged tx-pool submitter (RPC caller) can craft a transaction with high cycles and small serialized size. CKB scripts can be written to consume many cycles with minimal witness/output data. This is a realistic and reachable attack path requiring no special privileges.

---

### Recommendation

Replace the raw `size` argument in `calculate_min_replace_fee` with `get_transaction_weight(size, cycles)`. The caller `check_rbf` already has access to `entry.cycles`, so the fix is straightforward:

```rust
// In check_rbf:
self.calculate_min_replace_fee(&all_conflicted, entry.size, entry.cycles)

// In calculate_min_replace_fee:
fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize, cycles: Cycle) -> Option<Capacity> {
    let weight = get_transaction_weight(size, cycles);
    let extra_rbf_fee = self.config.min_rbf_rate.fee(weight);
    ...
}
```

This aligns the RBF fee enforcement with the weight metric used everywhere else in the tx-pool.

---

### Proof of Concept

1. Enable RBF on a CKB node (`min_rbf_rate > min_fee_rate`).
2. Submit a victim transaction `T1` to the pool with a moderate fee.
3. Craft a replacement transaction `T2` that:
   - Spends the same inputs as `T1`
   - Has a small serialized size (e.g., 100 bytes)
   - Executes a script consuming near-maximum cycles (e.g., 10,000,000 cycles)
   - Pays `T1.fee + min_rbf_rate.fee(100)` — only the size-based increment
4. Submit `T2` via the `send_transaction` RPC.
5. Observe that `T2` is accepted despite its effective fee rate (computed via `get_transaction_weight`) being far below `min_rbf_rate`, because `calculate_min_replace_fee` only checks `size`.

The inconsistency between `calculate_min_replace_fee` (uses `size`) and `AncestorsScoreSortKey`/`EvictKey` (use `get_transaction_weight`) means `T2` passes the RBF fee check but is sorted and evicted as if it has a much lower fee rate — exactly the inconsistency class described in the reference report.

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

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L469-472)
```rust
    pub fn accept_tx(&mut self, tx_hash: Byte32, info: TxEntryInfo) {
        let weight = get_transaction_weight(info.size as usize, info.cycles);
        let fee_rate = FeeRate::calculate(info.fee, weight);
        self.track_tx(tx_hash, fee_rate, self.current_tip)
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

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```
