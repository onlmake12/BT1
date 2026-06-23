### Title
Minimum Fee Check Ignores Cycles Dimension, Allowing High-Cycle Transactions to Underpay — (`tx-pool/src/util.rs`)

---

### Summary

The `check_tx_fee` function enforces the minimum fee rate using only the transaction's serialized byte size, completely ignoring the VM execution cycles dimension. Because CKB blocks are constrained by **two independent limits** — `MAX_BLOCK_BYTES` and `MAX_BLOCK_CYCLES` — a transaction that is small in bytes but consumes near-maximum cycles occupies a disproportionate share of block cycle space while paying a fee calibrated only to its byte footprint. This is the direct CKB analog of the xkeeper finding: a hard-coded fee formula that accounts for only one cost component while silently ignoring another that can be orders of magnitude larger.

---

### Finding Description

CKB transactions consume two independent block resources:

| Resource | Block Limit |
|---|---|
| Serialized size | `MAX_BLOCK_BYTES = 597,000 bytes` |
| VM execution cycles | `MAX_BLOCK_CYCLES = 3,500,000,000 cycles` |

The conversion ratio is `DEFAULT_BYTES_PER_CYCLES = MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES ≈ 0.000_170_571_4`, meaning 1 cycle ≈ 0.000170571 bytes. This ratio is used by `get_transaction_weight` to unify both dimensions into a single weight:

```rust
// util/types/src/core/tx_pool.rs
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

This weight is used for fee-rate sorting and prioritization throughout the pool. However, the **minimum fee admission check** in `check_tx_fee` does not use `get_transaction_weight`. It uses only `tx_size`:

```rust
// tx-pool/src/util.rs
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    ...
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    if fee < min_fee {
        return Err(Reject::LowFeeRate(...));
    }
    Ok(fee)
}
```

The comment explicitly acknowledges the theoretical incorrectness but treats it as an acceptable simplification. The default `min_fee_rate` is 1,000 shannons/KB, applied only to bytes.

**Concrete example with default config (`max_tx_verify_cycles = 70,000,000`):**

| Parameter | Value |
|---|---|
| Transaction size | 100 bytes |
| Transaction cycles | 70,000,000 |
| Cycles-equivalent bytes | 70,000,000 × 0.000_170_571_4 ≈ **11,940 bytes** |
| Actual weight (`get_transaction_weight`) | max(100, 11940) = **11,940** |
| Minimum fee (size-only check) | 1,000 × 100/1,000 = **100 shannons** |
| Minimum fee (weight-based, correct) | 1,000 × 11,940/1,000 = **11,940 shannons** |
| **Underpayment ratio** | **~119×** |

A transaction sender can pay 119× less than the weight-based minimum fee and still be admitted to the pool. The transaction then sits in the pool with an effective fee rate of ≈8.4 shannons/KW — far below the `min_fee_rate` of 1,000 shannons/KW — but it is not rejected.

The admission path is `pre_check` → `check_tx_fee` (size-only gate) → `verify_rtx` (cycles verified but not fee-checked) → pool entry:

```rust
// tx-pool/src/process.rs
let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
// cycles are verified separately but never cross-checked against fee
```

---

### Impact Explanation

1. **Miner under-compensation**: Any miner who includes a high-cycle, small-size transaction receives a fee calibrated to ~100 bytes of block space while the transaction actually consumes ~11,940 bytes-equivalent of cycle space. The miner's opportunity cost (foregone higher-fee transactions that could have used those cycles) is not reflected in the fee received.

2. **Fee market distortion**: The `min_fee_rate` parameter is documented and advertised as the threshold below which transactions are rejected. Operators set it expecting it to reflect actual resource cost. For cycle-heavy transactions, the effective admission threshold is up to ~119× lower than intended, breaking the fee market signal.

3. **Pool griefing**: An unprivileged submitter can flood the pool with minimum-fee, high-cycle transactions. Each occupies only ~100 bytes of the 180 MB pool limit (`max_tx_pool_size`) but consumes significant verification CPU (up to `max_tx_verify_cycles = 70M` cycles per tx). At 100 bytes each, ~1.8 million such transactions could be admitted before the byte-based pool limit is hit, each having consumed 70M cycles of verification work.

---

### Likelihood Explanation

The entry path requires only an unprivileged RPC call (`send_transaction`) or P2P relay. No special privileges, keys, or majority hashpower are needed. The attacker only needs to:
1. Construct a valid CKB transaction with a small serialized size
2. Include a script that consumes near-`max_tx_verify_cycles` cycles (e.g., a loop script)
3. Pay only `min_fee_rate × tx_size_bytes / 1000` shannons

This is straightforward for any script author. The discrepancy is largest for transactions with high cycles and small byte size — a class that includes many legitimate use cases (e.g., complex lock scripts with minimal data).

---

### Recommendation

Replace the size-only minimum fee check with a weight-based check that accounts for both dimensions:

```rust
// tx-pool/src/util.rs
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
    cycles: Cycle,  // add cycles parameter
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(...).transaction_fee(rtx)?;
    let weight = get_transaction_weight(tx_size, cycles);
    let min_fee = tx_pool.config.min_fee_rate.fee(weight);
    if fee < min_fee {
        return Err(Reject::LowFeeRate(...));
    }
    Ok(fee)
}
```

Since cycles are not known until after `verify_rtx`, the check should be split: a size-only pre-check before verification (as today), followed by a weight-based post-check after cycles are known. Alternatively, `min_fee_rate` can be redefined in terms of weight-units rather than bytes, with documentation updated accordingly.

---

### Proof of Concept

**Attacker-controlled entry path:**

1. Craft a CKB transaction:
   - One input cell, one output cell (minimal serialized size ≈ 100–200 bytes)
   - Lock script that loops for ~70,000,000 cycles (near `max_tx_verify_cycles`)
   - Fee = `min_fee_rate × tx_size / 1000` = 1,000 × 100/1,000 = **100 shannons**

2. Submit via `send_transaction` RPC or P2P relay.

3. `pre_check` calls `check_tx_fee(tx_pool, snapshot, rtx, tx_size=100)`:
   - `min_fee = 1000.fee(100) = 100 shannons`
   - `fee = 100 shannons`
   - `fee >= min_fee` → **passes**

4. `verify_rtx` executes the script, consuming 70,000,000 cycles. Cycles are verified against `max_tx_verify_cycles` only — no fee cross-check.

5. Transaction enters pool with:
   - `weight = get_transaction_weight(100, 70_000_000) = 11,940`
   - `fee_rate = FeeRate::calculate(100 shannons, 11940) ≈ 8 shannons/KW`
   - This is 125× below `min_fee_rate = 1,000 shannons/KW`

6. Repeat to fill the pool with 1.8M such transactions (bounded by 180 MB pool size), each having consumed 70M cycles of node verification work for 100 shannons of fee.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** util/app-config/src/configs/tx_pool.rs (L11-22)
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
```
