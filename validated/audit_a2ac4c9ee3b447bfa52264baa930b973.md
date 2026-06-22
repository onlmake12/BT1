### Title
Hardcoded `DEFAULT_BYTES_PER_CYCLES` Assumes Default Consensus Parameters, Causing Wrong Transaction Weight When `max_block_cycles` Differs — (`util/types/src/core/tx_pool.rs`)

---

### Summary

`get_transaction_weight` uses the compile-time constant `DEFAULT_BYTES_PER_CYCLES` (hardcoded as `MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES` for the **default** consensus parameters) to convert cycles into a byte-equivalent weight. However, `max_block_bytes` and `max_block_cycles` are configurable consensus parameters that can differ from the defaults. When they do, every transaction weight, fee-rate, and pool-ordering decision is computed against the wrong ratio, exactly mirroring the yVault `1e18`-vs-`decimals()` class of bug.

---

### Finding Description

`DEFAULT_BYTES_PER_CYCLES` is defined as a compile-time `f64` constant:

```rust
// util/types/src/core/tx_pool.rs  line 276-279
/// Equal to MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES, see ckb-chain-spec.
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;
``` [1](#0-0) 

Its value is derived from the **default** chain-spec constants:

- `MAX_BLOCK_BYTES = TWO_IN_TWO_OUT_BYTES × TWO_IN_TWO_OUT_COUNT = 597 × 1 000 = 597 000`
- `MAX_BLOCK_CYCLES = TWO_IN_TWO_OUT_CYCLES × TWO_IN_TWO_OUT_COUNT = 3 500 000 × 1 000 = 3 500 000 000`
- Ratio: `597 000 / 3 500 000 000 ≈ 0.000 170 571` [2](#0-1) 

Both `max_block_bytes` and `max_block_cycles` are **configurable** consensus parameters. The devnet spec already ships with a different value:

```toml
# resource/specs/dev.toml  line 89
max_block_cycles = 10_000_000_000   # ≈ 2.86× the default 3_500_000_000
``` [3](#0-2) 

The hardcoded constant is consumed unconditionally in `get_transaction_weight`:

```rust
// util/types/src/core/tx_pool.rs  line 298-303
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [4](#0-3) 

This function is called in every fee-rate and ordering path:

1. **Tx-pool entry fee rate** — `TxEntry::fee_rate()` and `AncestorsScoreSortKey` / `EvictKey` construction: [5](#0-4) [6](#0-5) 

2. **RPC fee-rate statistics** — `FeeRateCollector::statistics()`: [7](#0-6) 

3. **Fee estimator** — `weight_units_flow` algorithm: [8](#0-7) 

Additionally, the same estimator hardcodes `MAX_BLOCK_BYTES` (the constant, not the runtime consensus value) when computing how much weight is removed per block:

```rust
// util/fee-estimator/src/estimator/weight_units_flow.rs  line 284
let removed_weight = (MAX_BLOCK_BYTES * 85 / 100) * target_blocks;
``` [9](#0-8) 

---

### Impact Explanation

When `max_block_cycles` is larger than the default (e.g., devnet's `10 000 000 000`):

- **Actual** bytes-per-cycle = `597 000 / 10 000 000 000 ≈ 0.0000597`
- **Hardcoded** bytes-per-cycle = `0.000 170 571` — **2.86× too large**

A cycle-heavy transaction's weight is therefore **overestimated by ~2.86×**, making its computed fee rate appear **~2.86× lower** than it truly is. Consequences:

- **Tx-pool ordering is wrong**: high-cycle transactions are ranked lower than they deserve, causing miners to assemble suboptimal blocks and legitimate high-fee transactions to be evicted or delayed.
- **Fee estimation is wrong**: `estimate_fee_rate` returns inflated fee recommendations because the `removed_weight` per block is also computed from the wrong constant.
- **RPC fee-rate statistics are wrong**: callers of `get_fee_rate_statistics` receive incorrect data, leading wallets and users to overpay or underpay fees.

Conversely, when `max_block_cycles` is smaller than the default, cycle-heavy transactions are underweighted, allowing them to appear cheaper than they are and potentially crowd out byte-heavy transactions.

---

### Likelihood Explanation

The devnet chain spec already ships with `max_block_cycles = 10_000_000_000`, which is ~2.86× the default. Any operator running a private or development chain with non-default cycle limits — a supported and documented configuration — will silently get wrong fee-rate calculations. An unprivileged tx-pool submitter on such a network can craft cycle-heavy transactions that are systematically mis-priced, gaming pool ordering without any special privileges.

---

### Recommendation

Replace the compile-time constant with a runtime computation derived from the actual consensus parameters. Pass the `Consensus` (or its `max_block_bytes` / `max_block_cycles` fields) into `get_transaction_weight`, or compute the ratio once at node startup and store it alongside the consensus object:

```rust
// Instead of:
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}

// Use:
pub fn get_transaction_weight(tx_size: usize, cycles: u64, bytes_per_cycle: f64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * bytes_per_cycle) as u64)
}

// Where bytes_per_cycle = consensus.max_block_bytes() as f64 / consensus.max_block_cycles() as f64
```

Similarly, the `weight_units_flow` estimator should use `consensus.max_block_bytes()` rather than the compile-time `MAX_BLOCK_BYTES` constant when computing `removed_weight`.

---

### Proof of Concept

On a devnet node (`max_block_cycles = 10_000_000_000`, `max_block_bytes = 597_000`):

```
Actual bytes_per_cycle  = 597_000 / 10_000_000_000 = 0.0000597
Hardcoded               = 0.000_170_571_4

For a transaction with size=200 bytes and cycles=5_000_000:
  Correct weight  = max(200, 5_000_000 × 0.0000597)  = max(200, 298)  = 298
  Computed weight = max(200, 5_000_000 × 0.000170571) = max(200, 852)  = 852

Correct fee rate (fee=1000 shannons):   1000×1000/298  ≈ 3355 shannons/KW
Computed fee rate:                      1000×1000/852  ≈ 1174 shannons/KW

The transaction appears ~2.86× cheaper than it is, distorting pool ordering
and fee estimation for all cycle-heavy transactions.
```

### Citations

**File:** util/types/src/core/tx_pool.rs (L276-279)
```rust
/// Equal to MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES, see ckb-chain-spec.
/// The precision is set so that the difference between MAX_BLOCK_CYCLES * DEFAULT_BYTES_PER_CYCLES
/// and MAX_BLOCK_BYTES is less than 1.
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;
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

**File:** spec/src/consensus.rs (L70-84)
```rust
pub const TWO_IN_TWO_OUT_CYCLES: Cycle = 3_500_000;
/// bytes of a typical two-in-two-out tx.
pub const TWO_IN_TWO_OUT_BYTES: u64 = 597;
/// count of two-in-two-out txs a block should capable to package.
const TWO_IN_TWO_OUT_COUNT: u64 = 1_000;
pub(crate) const DEFAULT_EPOCH_DURATION_TARGET: u64 = 4 * 60 * 60; // 4 hours, unit: second
const MILLISECONDS_IN_A_SECOND: u64 = 1000;
const MAX_EPOCH_LENGTH: u64 = DEFAULT_EPOCH_DURATION_TARGET / MIN_BLOCK_INTERVAL; // 1800
const MIN_EPOCH_LENGTH: u64 = DEFAULT_EPOCH_DURATION_TARGET / MAX_BLOCK_INTERVAL; // 300
pub(crate) const DEFAULT_PRIMARY_EPOCH_REWARD_HALVING_INTERVAL: EpochNumber =
    4 * 365 * 24 * 60 * 60 / DEFAULT_EPOCH_DURATION_TARGET; // every 4 years

/// The default maximum allowed size in bytes for a block
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```

**File:** resource/specs/dev.toml (L89-89)
```text
max_block_cycles = 10_000_000_000
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
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

**File:** rpc/src/util/fee_rate.rs (L103-105)
```rust
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L97-101)
```rust
    fn new_from_entry_info(info: TxEntryInfo) -> Self {
        let weight = get_transaction_weight(info.size as usize, info.cycles);
        let fee_rate = FeeRate::calculate(info.fee, weight);
        Self { weight, fee_rate }
    }
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L284-284)
```rust
            let removed_weight = (MAX_BLOCK_BYTES * 85 / 100) * target_blocks;
```
