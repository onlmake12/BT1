### Title
Tx-Pool Minimum Fee Rate Check Uses Serialized Size Instead of Weight, Allowing High-Cycle Transactions With Economically Unviable Fee Rates to Accumulate — (`tx-pool/src/util.rs`)

---

### Summary

The `check_tx_fee` function in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size. However, the actual transaction **weight** used by miners for block assembly is `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. Because cycles are only known after script execution, no weight-based fee check is performed after verification. This allows an attacker to submit transactions with small serialized size (passing the size-based gate) but very high cycle consumption, resulting in an effective weight-based fee rate far below `min_fee_rate`. These transactions are admitted to the pool, consume verification resources, and are never mined — directly analogous to the "no minimum threshold" class of vulnerability.

---

### Finding Description

**Root cause — `tx-pool/src/util.rs`, `check_tx_fee`:**

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [1](#0-0) 

The minimum fee is computed as `min_fee_rate * tx_size`, not `min_fee_rate * weight`.

**Actual weight formula — `util/types/src/core/tx_pool.rs`:**

```rust
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

The weight is `max(size, cycles * 0.000_170_571_4)`. For a high-cycle transaction, weight >> size.

**Admission flow — `tx-pool/src/process.rs`:**

`pre_check` calls `check_tx_fee` with `tx_size` **before** script execution. After `verify_rtx` returns the actual cycles, the entry is created with `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — but no second weight-based fee check is performed. [3](#0-2) 

**Fee rate used for mining priority — `tx-pool/src/component/entry.rs`:**

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [4](#0-3) 

Miners sort by this weight-based fee rate, not the size-based one used at admission.

---

### Impact Explanation

**Concrete numbers with default config (`min_fee_rate = 1000`, `max_tx_verify_cycles = 70,000,000`):**

| Metric | Value |
|---|---|
| Minimum tx size | ~300 bytes |
| Max cycles | 70,000,000 |
| Max weight from cycles | `70,000,000 × 0.000_170_571_4 ≈ 11,940` |
| Effective weight | `max(300, 11940) = 11,940` |
| Size-based min fee (passes gate) | `1000 × 300 / 1000 = 300 shannons` |
| Weight-based min fee (never checked) | `1000 × 11940 / 1000 = 11,940 shannons` |
| Effective fee rate by weight | `300 × 1000 / 11940 ≈ 25 shannons/KW` |

A transaction paying only 300 shannons passes admission but has an effective fee rate of ~25 shannons/KW — **40× below** the 1000 shannons/KW minimum. Miners will never select it. Such transactions:

1. Are admitted to the pool and consume verification resources (up to 70M cycles each).
2. Have the lowest possible mining priority.
3. Persist in the pool for up to 12 hours (`expiry_hours = 12`) before expiry.
4. Displace legitimate transactions when the pool approaches `max_tx_pool_size = 180 MB`. [5](#0-4) 

---

### Likelihood Explanation

The attacker needs:
- A deployed script (or use of an existing always-success-like script) that consumes many cycles.
- A transaction with small serialized size referencing that script.
- A fee of only ~300 shannons (trivial cost).

This is reachable via the `send_transaction` RPC by any unprivileged user. No special privileges, keys, or majority hashpower are required. The `max_tx_verify_cycles` limit bounds per-transaction cost but does not prevent accumulation across many transactions. [6](#0-5) 

---

### Recommendation

After `verify_rtx` returns the actual cycles, perform a second fee rate check using the true weight:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```

This mirrors how `fee_rate()` is computed on `TxEntry` and how miners actually evaluate transactions. [4](#0-3) 

---

### Proof of Concept

1. Deploy a lock script that runs a tight loop consuming ~70,000,000 cycles.
2. Create a cell locked by that script (small output, ~300 bytes serialized).
3. Submit a transaction spending that cell with fee = 300 shannons (passes `check_tx_fee` since `1000 * 300 / 1000 = 300`).
4. The transaction passes `pre_check`, enters the verify queue, consumes 70M cycles during `verify_rtx`, and is admitted to the pool.
5. Its `fee_rate()` = `FeeRate::calculate(300, 11940)` ≈ 25 shannons/KW — never selected by miners.
6. Repeat to fill the pool with permanently-unmined transactions at minimal cost. [7](#0-6) [8](#0-7)

### Citations

**File:** tx-pool/src/util.rs (L28-54)
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
}
```

**File:** util/types/src/core/tx_pool.rs (L279-303)
```rust
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

**File:** tx-pool/src/process.rs (L269-315)
```rust
    pub(crate) async fn pre_check(
        &self,
        tx: &TransactionView,
    ) -> (Result<PreCheckedTx, Reject>, Arc<Snapshot>) {
        // Acquire read lock for cheap check
        let tx_size = tx.data().serialized_size_in_block();

        let (ret, snapshot) = self
            .with_tx_pool_read_lock(|tx_pool, snapshot| {
                let tip_hash = snapshot.tip_hash();

                // Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc.
                // It's also not possible for RBF, reject it directly
                check_txid_collision(tx_pool, tx)?;

                // Try normal path first, if double-spending check success we don't need RBF check
                // this make sure RBF won't introduce extra performance cost for hot path
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
                        if conflicts.is_none() {
                            // this mean one input's outpoint is dead, but there is no direct conflicted tx in tx_pool
                            // we should reject it directly and don't need to put it into conflicts pool
                            error!(
                                "{} is resolved as Dead, but there is no conflicted tx",
                                rtx.transaction.proposal_short_id()
                            );
                            return Err(Reject::Resolve(OutPointError::Dead(out)));
                        }
                        // we also return Ok here, so that the entry will be continue to be verified before submit
                        // we only want to put it into conflicts pool after the verification stage passed
                        // then we will double-check conflicts txs in `submit_entry`

                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(err) => Err(err),
                }
            })
            .await;
        (ret, snapshot)
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** util/app-config/src/legacy/tx_pool.rs (L9-20)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
// default max tx verify cycles
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
// default max ancestors count
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
// Default expiration time for pool transactions in hours
const DEFAULT_EXPIRY_HOURS: u8 = 12;
// Default max_tx_pool_size 180mb
const DEFAULT_MAX_TX_POOL_SIZE: usize = 180_000_000;
```

**File:** util/app-config/src/configs/tx_pool.rs (L11-43)
```rust
pub struct TxPoolConfig {
    /// Keep the transaction pool below <max_tx_pool_size> mb
    pub max_tx_pool_size: usize,
    /// txs with lower fee rate than this will not be relayed or be mined
    #[serde(with = "FeeRateDef")]
    pub min_fee_rate: FeeRate,
    /// txs need to pay larger fee rate than this for RBF
    #[serde(with = "FeeRateDef")]
    pub min_rbf_rate: FeeRate,
    /// tx pool rejects txs that cycles greater than max_tx_verify_cycles
    pub max_tx_verify_cycles: Cycle,
    /// max tx verify workers, default is 3/4 of cpu cores
    #[serde(default = "default_max_tx_verify_workers")]
    pub max_tx_verify_workers: usize,
    /// max ancestors size limit for a single tx
    pub max_ancestors_count: usize,
    /// rejected tx time to live by days
    pub keep_rejected_tx_hashes_days: u8,
    /// rejected tx count limit
    pub keep_rejected_tx_hashes_count: u64,
    /// The file to persist the tx pool on the disk when tx pool have been shutdown.
    ///
    /// By default, it is a subdirectory of 'tx-pool' subdirectory under the data directory.
    #[serde(default)]
    pub persisted_data: PathBuf,
    /// The recent reject record database directory path.
    ///
    /// By default, it is a subdirectory of 'tx-pool' subdirectory under the data directory.
    #[serde(default)]
    pub recent_reject: PathBuf,
    /// The expiration time for pool transactions in hours
    pub expiry_hours: u8,
}
```
