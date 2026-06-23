### Title
Fixed `tx_size`-Only Weight in `check_tx_fee` Allows High-Cycle Transactions to Bypass Effective Minimum Fee Rate — (`tx-pool/src/util.rs`)

---

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size as the weight. However, the actual fee rate used for pool sorting and eviction is computed via `get_transaction_weight(tx_size, cycles)`, which takes `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For a high-cycle transaction, the actual weight can be up to ~60× larger than the byte size alone. This means the effective minimum fee rate enforced at admission is up to 60× lower than the configured `min_fee_rate`, allowing an unprivileged RPC caller to flood the tx-pool with transactions that pass the admission check but carry actual fee rates far below the minimum.

---

### Finding Description

The admission check in `check_tx_fee` computes the minimum required fee using only `tx_size`: [1](#0-0) 

```rust
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

`FeeRate::fee` computes `min_fee_rate * tx_size / 1000`: [2](#0-1) 

But the actual fee rate used for pool ordering and eviction is computed via `get_transaction_weight`, which takes the **maximum** of size and cycle-derived weight: [3](#0-2) 

```rust
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

`DEFAULT_BYTES_PER_CYCLES` is a hardcoded constant equal to `MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES`. For a transaction with `max_tx_verify_cycles = 70_000_000` cycles and a minimal serialized size of ~200 bytes:

- **Admission weight** (size-only): `200`
- **Actual weight** (cycle-derived): `max(200, 70_000_000 × 0.000_170_571_4) = max(200, 11_940) = 11_940`
- **Minimum fee enforced at admission**: `1000 × 200 / 1000 = 200 shannons`
- **Actual fee rate of admitted transaction**: `200 × 1000 / 11_940 ≈ 16 shannons/KW`

The configured `min_fee_rate` is 1000 shannons/KW, but the transaction enters the pool with an actual fee rate of only ~16 shannons/KW — a **~60× discount**.

The `check_tx_fee` function signature does not even accept a `cycles` parameter, so it structurally cannot perform the correct weight-based check: [4](#0-3) 

The default `min_fee_rate` and `max_tx_verify_cycles` are: [5](#0-4) 

---

### Impact Explanation

An attacker who deploys a script that consumes close to `max_tx_verify_cycles` (70M) cycles but produces a small transaction can repeatedly submit transactions that:

1. Pass `check_tx_fee` by paying only the size-based minimum fee (e.g., 200 shannons).
2. Enter the tx-pool with an actual fee rate ~60× below `min_fee_rate`.
3. Occupy pool weight far in excess of what the fee paid would normally allow.

With a 180 MB pool and ~11,940 bytes of effective weight per transaction, the attacker can fill the pool with ~15,000 transactions at a total cost of ~3,000,000 shannons (0.03 CKB). This:

- **Evicts legitimate transactions** that have higher actual fee rates but lower absolute fees, causing them to be rejected when the pool is full.
- **Degrades fee estimation** by polluting the pool with artificially low-fee-rate entries.
- **Wastes miner block space** if these transactions are included, reducing effective throughput.

---

### Likelihood Explanation

- The attack is reachable by any unprivileged `send_transaction` RPC caller.
- No special privilege, key, or majority hashpower is required.
- The cost is extremely low (~0.03 CKB to fill the pool).
- The attacker only needs to deploy one high-cycle script (a one-time cost), then submit many cheap transactions referencing it.
- The discrepancy is structural: `check_tx_fee` lacks a `cycles` parameter and cannot be fixed without a signature change.

---

### Recommendation

Pass the declared cycle count into `check_tx_fee` and compute the minimum fee using `get_transaction_weight(tx_size, cycles)` instead of `tx_size` alone:

```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
    cycles: Cycle,          // add cycles parameter
) -> Result<Capacity, Reject> {
    ...
    let weight = get_transaction_weight(tx_size, cycles);
    let min_fee = tx_pool.config.min_fee_rate.fee(weight);
    ...
}
```

This ensures the admission check enforces the same fee rate floor that the pool's sorting and eviction logic uses, eliminating the ~60× discount available to high-cycle transactions.

---

### Proof of Concept

1. Deploy a CKB script that loops until it consumes ~70,000,000 cycles but whose serialized transaction size is ~200 bytes.
2. Call `send_transaction` RPC with this transaction and fee = `min_fee_rate × 200 / 1000 = 200` shannons (just above the size-based minimum).
3. Observe the transaction is accepted into the pool.
4. Query `get_raw_tx_pool` and compute the actual fee rate: `200 × 1000 / 11940 ≈ 16 shannons/KW`, far below the configured `min_fee_rate = 1000`.
5. Repeat step 2 ~15,000 times to fill the 180 MB pool at a total cost of ~3,000,000 shannons (0.03 CKB).
6. Observe that legitimate transactions with higher actual fee rates but lower absolute fees are rejected with `PoolIsFull`.

### Citations

**File:** tx-pool/src/util.rs (L28-33)
```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
```

**File:** tx-pool/src/util.rs (L42-52)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
    }
```

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```

**File:** util/types/src/core/tx_pool.rs (L276-303)
```rust
/// Equal to MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES, see ckb-chain-spec.
/// The precision is set so that the difference between MAX_BLOCK_CYCLES * DEFAULT_BYTES_PER_CYCLES
/// and MAX_BLOCK_BYTES is less than 1.
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

/// vbytes has been deprecated, renamed to weight to prevent ambiguity
#[deprecated(
    since = "0.107.0",
    note = "Please use the get_transaction_weight instead"
)]
pub fn get_transaction_virtual_bytes(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}

/// The miners select transactions to fill the limited block space which gives the highest fee.
/// Because there are two different limits, serialized size and consumed cycles,
/// the selection algorithm is a multi-dimensional knapsack problem.
/// Introducing the transaction weight converts the multi-dimensional knapsack to a typical knapsack problem,
/// which has a simple greedy algorithm.
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** util/app-config/src/legacy/tx_pool.rs (L9-16)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
// default max tx verify cycles
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
// default max ancestors count
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
```
