### Title
Ineffective `min_fee_rate` Check Due to Size-Only Weight Calculation Allows Fee-Rate Bypass — (`File: tx-pool/src/util.rs`)

### Summary

The `check_tx_fee` function in CKB's tx-pool admission path computes the minimum required fee using only the serialized transaction **size**, not the true `weight` (which is `max(size, cycles * DEFAULT_BYTES_PER_CYCLES)`). An attacker can craft a transaction with a tiny serialized size but a very high declared cycle count, satisfying the size-based fee check while paying a fee rate that is far below the actual `min_fee_rate` when measured against the true weight. This allows an unprivileged tx-pool submitter or relay peer to flood the pool with economically undersized transactions, degrading node performance.

---

### Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` enforces the minimum fee rate using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [1](#0-0) 

The comment explicitly acknowledges this is a deliberate approximation ("cheap check"). However, the actual weight used everywhere else in the system — for fee-rate scoring, eviction, and block assembly — is computed as:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

When a transaction has a small serialized size but a large cycle count (up to `max_block_cycles`), its true weight is dominated by cycles. The admission check uses only `tx_size`, so the fee required to pass admission is `min_fee_rate * tx_size / 1000`, but the true fee rate (used for eviction and scoring) is `fee / (cycles * DEFAULT_BYTES_PER_CYCLES)` — which can be orders of magnitude lower than `min_fee_rate`.

**Concrete example:**
- `min_fee_rate` = 1000 shannons/KW (default)
- A transaction with `tx_size` = 300 bytes and `declared_cycles` = 70,000,000 (the default `max_tx_verify_cycles`)
- True weight = `max(300, 70_000_000 * 0.000_170_571_4)` ≈ `max(300, 11,940)` = **11,940**
- Fee required by admission check: `1000 * 300 / 1000` = **300 shannons**
- Actual fee rate at admission: `300 * 1000 / 11940` ≈ **25 shannons/KW** — far below `min_fee_rate`

Such a transaction passes `check_tx_fee` but has an effective fee rate ~40× below the configured minimum.

The `TxEntry::fee_rate()` method, used for eviction and scoring, correctly uses `get_transaction_weight`:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [3](#0-2) 

So the pool correctly scores and evicts these transactions by true weight — but only **after** they are admitted and fully verified (including script execution up to `max_tx_verify_cycles`).

The attack path for remote peers goes through `submit_remote_tx` → `resumeble_process_tx` → `pre_check` (which calls `check_tx_fee` with size only) → `verify_rtx` (full script execution). [4](#0-3) 

For large-cycle transactions, they are routed through the async `VerifyQueue` before being admitted. The `VerifyQueue` has a 256 MB size-based cap, but no cycle-based cap. [5](#0-4) 

---

### Impact Explanation

1. **Verify queue saturation**: An attacker submits many small-size, high-cycle transactions that pass the size-based fee check. Each transaction occupies minimal space in the `VerifyQueue` (size-based limit) but consumes maximum CPU time during script verification (up to `max_tx_verify_cycles` per tx). This can saturate the async verify workers, delaying legitimate transaction processing.

2. **Pool flooding with underpriced transactions**: Admitted transactions with true fee rates far below `min_fee_rate` occupy pool space and are only evicted when the pool is full — at which point they may displace legitimate transactions with higher true fee rates if the eviction ordering is perturbed.

3. **Fee estimation corruption**: The `WeightUnitsFlow` and `ConfirmationFraction` fee estimators receive these underpriced entries via `accept_tx`, skewing fee rate statistics downward. [6](#0-5) 

---

### Likelihood Explanation

- Entry path is fully open: any RPC caller via `send_transaction` or any relay peer via `RelayV3` `SendRelayTransactions` can submit transactions with attacker-controlled cycle declarations.
- The `max_block_cycles` check in `TransactionsProcess` only rejects cycles **greater than** `max_block_cycles`; cycles equal to `max_tx_verify_cycles` (70M, default) are accepted. [7](#0-6) 
- No privileged access is required. The attacker only needs valid UTXOs to spend (to construct valid transactions) and the ability to connect to a node.
- The discrepancy between size-based admission and weight-based scoring is explicitly noted in a code comment as a known approximation, indicating awareness but no mitigation.

---

### Recommendation

Replace the size-only fee check in `check_tx_fee` with a weight-based check. Since the declared cycle count is available at `pre_check` time (passed as `declared_cycles` for remote transactions), compute the true weight before checking:

```rust
// Use true weight (max of size and cycle-equivalent bytes) for fee rate check
let weight = get_transaction_weight(tx_size, declared_cycles.unwrap_or(0));
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

For local transactions where cycles are unknown at pre-check time, use the size-based check as a lower bound but add a post-verification re-check using the actual verified cycle count before final admission.

---

### Proof of Concept

1. Obtain a live cell with sufficient capacity (e.g., 10,000 shannons).
2. Construct a transaction spending that cell with:
   - A lock script that consumes exactly `max_tx_verify_cycles - 1` cycles (e.g., a tight loop in CKB-VM).
   - Output capacity = input capacity − 300 shannons (fee = 300 shannons).
   - Serialized size ≈ 300 bytes.
3. Submit via `send_transaction` RPC to a node with `min_fee_rate = 1000`.
4. The transaction passes `check_tx_fee` (300 shannons ≥ `1000 * 300 / 1000` = 300 shannons).
5. True fee rate = `300 * 1000 / 11940` ≈ 25 shannons/KW — 40× below `min_fee_rate`.
6. Repeat with many independent UTXOs. Each transaction enters the `VerifyQueue`, consuming a full verification slot for `max_tx_verify_cycles` cycles, while paying only the size-based minimum fee. [8](#0-7) [9](#0-8)

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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
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

**File:** tx-pool/src/component/verify_queue.rs (L17-18)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L153-162)
```rust
    pub fn accept_tx(&mut self, info: TxEntryInfo) {
        if self.current_tip == 0 {
            return;
        }
        let item = TxStatus::new_from_entry_info(info);
        self.txs
            .entry(self.current_tip)
            .and_modify(|items| items.push(item))
            .or_insert_with(|| vec![item]);
    }
```

**File:** sync/src/relayer/transactions_process.rs (L63-74)
```rust
        let max_block_cycles = self.relayer.shared().consensus().max_block_cycles();
        if txs
            .iter()
            .any(|(_, declared_cycles)| declared_cycles > &max_block_cycles)
        {
            self.nc.ban_peer(
                self.peer,
                DEFAULT_BAN_TIME,
                String::from("relay declared cycles greater than max_block_cycles"),
            );
            return Status::ok();
        }
```
