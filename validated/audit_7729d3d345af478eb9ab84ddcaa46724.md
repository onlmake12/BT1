### Title
Tx-Pool Minimum Fee Rate Gate Uses Serialized Size Instead of Weight, Allowing High-Cycle Transactions to Bypass the Pre-Check and Force Expensive Script Verification - (File: tx-pool/src/util.rs)

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the `min_fee_rate` threshold using only the transaction's serialized byte size, while the rest of the pool (scoring, eviction, fee rate display) uses the correct **weight** metric (`max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`). An unprivileged tx-pool submitter can craft a transaction with a very small serialized size but near-maximum cycle consumption, pay a fee just above `min_fee_rate * tx_size`, pass the cheap pre-check gate, and force the node to perform full, expensive script verification — work that should have been rejected cheaply. The code itself acknowledges the discrepancy with a comment but treats it as an acceptable approximation, which it is not when cycles dominate weight.

### Finding Description

In `check_tx_fee`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [1](#0-0) 

The correct weight formula used everywhere else in the pool is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. With `max_tx_verify_cycles = 70_000_000`, the maximum cycle-derived weight is `70_000_000 × 0.000_170_571_4 ≈ 11_940` bytes.

`check_tx_fee` is called in `pre_check` **before** script execution, so cycles are not yet known:

```rust
let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
``` [3](#0-2) 

After verification, the entry is created with the actual cycles and admitted to the pool:

```rust
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
``` [4](#0-3) 

The pool's eviction and scoring correctly use `get_transaction_weight(self.size, self.cycles)`:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [5](#0-4) 

### Impact Explanation

A tx-pool submitter can craft a transaction with:
- Serialized size: ~200 bytes (small)
- Cycle consumption: ~70,000,000 (near `max_tx_verify_cycles`)
- Fee: `min_fee_rate × tx_size = 1000 × 200 / 1000 = 200 shannons`

The actual weight is `max(200, 11_940) = 11_940` bytes. The correct minimum fee should be `1000 × 11_940 / 1000 = 11_940 shannons`. The transaction pays only 200 shannons — ~60× below the intended minimum — yet passes `check_tx_fee`.

The node then performs full script verification consuming up to 70M cycles of CPU. The purpose of the pre-check gate is precisely to avoid this expensive work for under-priced transactions. By bypassing it, an attacker with valid UTXOs can continuously force expensive verification, constituting a resource exhaustion / DoS vector against the node. [6](#0-5) 

### Likelihood Explanation

The attack is reachable by any unprivileged actor who can submit transactions via the `send_transaction` RPC or the P2P relay protocol. The attacker needs valid UTXOs to spend, which limits sustained attacks but does not prevent them. The discrepancy between size-based and weight-based fee rate can be up to ~60× under default consensus parameters, making the bypass straightforward to engineer. [7](#0-6) 

### Recommendation

Compute the minimum fee in `check_tx_fee` using the weight formula rather than raw size. Since cycles are not yet known at pre-check time, use a conservative upper bound: `get_transaction_weight(tx_size, max_tx_verify_cycles)`. This ensures that any transaction that could possibly consume maximum cycles is required to pay the correspondingly higher fee before expensive verification is triggered.

```rust
let max_possible_weight = get_transaction_weight(tx_size, tx_pool.config.max_tx_verify_cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(max_possible_weight);
```

Alternatively, apply a secondary fee rate check after verification (when actual cycles are known) and reject the transaction at that point if its effective fee rate is below `min_fee_rate`.

### Proof of Concept

1. Obtain a UTXO with capacity C.
2. Construct a transaction with:
   - One input spending that UTXO
   - One output with capacity `C - 200` shannons (fee = 200 shannons)
   - A lock/type script that consumes ~70,000,000 cycles
   - Serialized size ~200 bytes
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200 shannons`; the transaction passes.
5. The node performs full script verification consuming ~70M cycles.
6. The transaction is admitted to the pool with effective fee rate ≈ 17 shannons/KW, far below the configured `min_fee_rate` of 1000 shannons/KW.
7. Repeat with additional UTXOs to continuously exhaust node CPU. [8](#0-7) [9](#0-8)

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

**File:** tx-pool/src/process.rs (L269-316)
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
    }
```

**File:** tx-pool/src/process.rs (L751-751)
```rust
        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
```

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** util/app-config/src/configs/tx_pool.rs (L14-22)
```rust
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
