### Title
`check_tx_fee` Admits High-Cycle Transactions Using Size-Only Fee Check, Bypassing True Weight-Based Minimum Fee — (`File: tx-pool/src/util.rs`)

### Summary

The tx-pool admission gate in `check_tx_fee` computes the minimum required fee using only the transaction's serialized byte size, while the actual resource cost used for block packing and fee-rate prioritization is `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. A transaction sender can craft a small-serialized but high-cycle transaction that passes the admission check while paying a fee far below what its true weight demands, analogous to the original report's use of a user-controlled `tx.gasPrice` that is insufficient at execution time.

---

### Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` enforces the minimum fee rate using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The actual transaction weight used everywhere else — for pool scoring, miner selection, and fee-rate calculation — is defined as:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. [3](#0-2) 

The `TxEntry::fee_rate()` method — used for pool prioritization and eviction — uses the full weight:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [4](#0-3) 

The gap between the two metrics is large. With `min_fee_rate = 1000` shannons/KW (the default):

- A transaction with `size = 200` bytes and `cycles = 5_000_000` has:
  - Admission check weight: `200` → `min_fee = 200 shannons`
  - Actual weight: `max(200, 5_000_000 × 0.000_170_571_4) ≈ max(200, 852) = 852`
  - True min fee by weight: `852 shannons`
- A transaction with `size = 200` bytes and `cycles = 70_000_000` (max block cycles) has:
  - Admission check weight: `200` → `min_fee = 200 shannons`
  - Actual weight: `max(200, 70_000_000 × 0.000_170_571_4) ≈ 11,940`
  - True min fee by weight: `11,940 shannons`

The transaction passes `check_tx_fee` paying only 200 shannons but its actual weight-based cost is ~60× higher.

The `_process_tx` flow confirms: `check_tx_fee` runs during `pre_check` (before script execution), and the `TxEntry` is created with the verified `cycles` only after execution — but the fee gate has already been passed. [5](#0-4) 

---

### Impact Explanation

**Impact: Medium**

An unprivileged tx-pool submitter (via `send_transaction` RPC or P2P relay) can craft transactions with scripts that consume many cycles but have a small serialized size. Such transactions:

1. **Pass the fee admission gate** paying only `min_fee_rate × size` shannons.
2. **Enter the pool with an effective fee rate far below `min_fee_rate`** when measured by actual weight.
3. **Consume pool resources** (memory, verification CPU) at a fraction of the intended cost.
4. **Degrade miner revenue**: miners selecting by `fee_rate()` (weight-based) will deprioritize or never include these transactions, yet they occupy pool space and displace legitimately-priced transactions.
5. **Pool spam**: an attacker can flood the pool with high-cycle, low-fee transactions at ~60× lower cost than intended, causing legitimate transactions to be evicted via the `Full` reject path. [6](#0-5) 

---

### Likelihood Explanation

**Likelihood: Medium**

The attack requires only the ability to submit transactions to a CKB node's RPC or P2P relay interface — no privileged access. Crafting a transaction with a script that consumes many cycles but serializes to a small size is straightforward (e.g., a tight loop in a lock script). The default `min_fee_rate = 1000` shannons/KB and `max_tx_verify_cycles = 70_000_000` make the gap exploitable on any standard node. [7](#0-6) 

---

### Recommendation

Replace the size-only fee check in `check_tx_fee` with a weight-based check. Since cycles are not yet known at `pre_check` time (scripts haven't run), use the declared cycles (for remote transactions) or the configured `max_tx_verify_cycles` as a conservative upper bound for the weight calculation, then re-validate after actual execution. Alternatively, perform a second fee-rate check after `verify_rtx` returns the actual cycles, before calling `submit_entry`. [8](#0-7) 

---

### Proof of Concept

1. Craft a CKB transaction with a lock script that runs a tight loop consuming ~`max_tx_verify_cycles` (e.g., 70,000,000) cycles but whose serialized size is minimal (~200 bytes).
2. Set the transaction fee to exactly `min_fee_rate × tx_size = 1000 × 200 / 1000 = 200 shannons`.
3. Submit via `send_transaction` RPC.
4. Observe: the transaction is accepted into the pool (`check_tx_fee` passes with `fee=200 >= min_fee=200`).
5. Query `get_raw_tx_pool` with `verbose=true` and compute `fee / weight` where `weight = max(200, 70_000_000 × 0.000_170_571_4) ≈ 11,940`.
6. Effective fee rate ≈ `200 × 1000 / 11940 ≈ 16 shannons/KW` — far below the `min_fee_rate` of `1000 shannons/KW`.
7. Repeat to fill the pool with such transactions at ~60× lower cost than intended, evicting legitimately-priced transactions. [9](#0-8) [10](#0-9)

### Citations

**File:** tx-pool/src/util.rs (L28-53)
```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
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
    Ok(fee)
```

**File:** util/types/src/core/tx_pool.rs (L33-34)
```rust
    #[error("Transaction is replaced because the pool is full, {0}")]
    Full(String),
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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/process.rs (L715-751)
```rust
        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);

        let verify_cache = self.fetch_tx_verify_cache(&tx).await;
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
        let tip_header = snapshot.tip_header();
        let tx_env = Arc::new(status.with_env(tip_header));

        let verified_ret = verify_rtx(
            Arc::clone(&snapshot),
            Arc::clone(&rtx),
            tx_env,
            &verify_cache,
            max_cycles,
            command_rx,
        )
        .await;

        let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

        if let Some(declared) = declared_cycles
            && declared != verified.cycles
        {
            info!(
                "process_tx declared cycles not match verified cycles, declared: {}, verified: {}, tx_hash: {}",
                declared,
                verified.cycles,
                tx.hash()
            );
            return Some((
                Err(Reject::DeclaredWrongCycles(declared, verified.cycles)),
                snapshot,
            ));
        }

        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
```

**File:** resource/ckb.toml (L212-215)
```text
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
max_tx_verify_cycles = 70_000_000
```
