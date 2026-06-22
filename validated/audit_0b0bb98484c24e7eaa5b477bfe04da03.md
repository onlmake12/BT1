### Title
Minimum Fee Rate Check Uses Only Serialized Size, Ignoring Cycles — Allows Sub-Minimum-Fee Transactions Into the Tx-Pool - (File: `tx-pool/src/util.rs`)

---

### Summary

The `check_tx_fee` function enforces the minimum fee rate gate using only the transaction's serialized byte size as the weight denominator. However, the actual effective fee rate stored in every `TxEntry` — and used for block assembly, eviction, and fee estimation — is computed with `get_transaction_weight(size, cycles) = max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. A transaction sender can craft a transaction with a small serialized size but very high script execution cycles, pay a fee that satisfies the size-only check, and be admitted to the tx-pool with an effective fee rate orders of magnitude below the configured minimum.

---

### Finding Description

**Root cause — `check_tx_fee` in `tx-pool/src/util.rs`:**

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [1](#0-0) 

The comment explicitly acknowledges the discrepancy but treats it as acceptable. The gate computes `min_fee = min_fee_rate × tx_size`, ignoring cycles entirely.

**Actual weight used everywhere else — `get_transaction_weight` in `util/types/src/core/tx_pool.rs`:**

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`, derived from `MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES`.

**Effective fee rate stored in `TxEntry` — `tx-pool/src/component/entry.rs`:**

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [3](#0-2) 

**Processing pipeline — `tx-pool/src/process.rs`:**

`pre_check` calls `check_tx_fee` (size-only gate) **before** script verification. Actual cycles are only known after `verify_rtx` returns. The `TxEntry` is then constructed with the real cycles, giving it a true fee rate that was never validated against the minimum. [4](#0-3) 

**Concrete numbers:**

| Parameter | Value |
|---|---|
| `min_fee_rate` (default) | 1,000 shannons/KB |
| `DEFAULT_BYTES_PER_CYCLES` | 0.000_170_571_4 |
| `max_tx_verify_cycles` (config) | 70,000,000 |

Craft a transaction with `tx_size = 200 bytes`, `cycles = 70,000,000`:

- **Gate check**: `min_fee = 1,000 × 200 / 1,000 = 200 shannons` → pay exactly 200 shannons → **passes**
- **Actual weight**: `max(200, 70,000,000 × 0.000_170_571_4) ≈ max(200, 11,940) = 11,940`
- **Effective fee rate**: `200 × 1,000 / 11,940 ≈ 16 shannons/KB` — **59× below the minimum**

---

### Impact Explanation

1. **Tx-pool flooding with sub-minimum-rate transactions**: An attacker submits many transactions that pass the size-only gate but carry an effective fee rate far below `min_fee_rate`. The pool fills with entries that should have been rejected, displacing legitimate transactions when the pool reaches `max_tx_pool_size`.

2. **CPU resource exhaustion**: Each admitted transaction forces the node to run the full CKB-VM script verifier for up to `max_block_cycles` cycles. Paying only the size-based minimum fee, an attacker can trigger disproportionately expensive verification work per shannon spent.

3. **Fee estimation distortion**: The `estimate_fee_rate` fallback path (`pool_map.estimate_fee_rate`) iterates pool entries by their stored `fee_rate()`, which uses the real weight. A pool flooded with artificially low-rate entries skews the fee estimate downward, misleading honest users about the true market rate. [5](#0-4) 

---

### Likelihood Explanation

The attack requires no special privilege. Any node reachable via the JSON-RPC `send_transaction` endpoint or the P2P relay protocol (`RelayTransactions`) can submit such transactions. The only cost is the size-based minimum fee (e.g., 200 shannons per transaction in the example above). The attacker can sustain a continuous stream of high-cycle, small-size transactions at negligible cost, making this a practical and repeatable attack.

For P2P relay, the declared cycles must match the actual cycles (enforced by `DeclaredWrongCycles`), but the attacker simply declares the true high cycle count — the fee check still uses only size. [6](#0-5) 

---

### Recommendation

Replace the size-only minimum fee gate in `check_tx_fee` with a weight-aware check. For remote transactions where `declared_cycles` is available before verification, use it immediately:

```rust
// Use declared_cycles if available; otherwise fall back to size-only as a
// conservative lower bound (actual cycles checked post-verification).
let weight = if let Some(cycles) = declared_cycles {
    get_transaction_weight(tx_size, cycles)
} else {
    tx_size as u64
};
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

For local transactions (no declared cycles), a post-verification check should be added in `_process_tx` after `verified.cycles` is known, rejecting the entry if its true effective fee rate falls below `min_fee_rate`.

---

### Proof of Concept

1. Construct a CKB transaction whose lock script runs a tight computation loop consuming ~70,000,000 cycles but whose serialized size is ~200 bytes.
2. Set outputs capacity so that `inputs_capacity − outputs_capacity = 200 shannons` (exactly `min_fee_rate × tx_size`).
3. Submit via `send_transaction` RPC.
4. Observe the transaction is accepted into the pending pool.
5. Query `get_transaction` and inspect the entry: the stored cycles will be ~70,000,000, giving an effective fee rate of ~16 shannons/KB — well below the 1,000 shannons/KB minimum.
6. Repeat in a loop; each iteration costs ~200 shannons but forces ~70,000,000 cycles of VM execution on the node. [7](#0-6) [8](#0-7) [9](#0-8)

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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
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

**File:** tx-pool/src/component/pool_map.rs (L334-358)
```rust
    pub(crate) fn estimate_fee_rate(
        &self,
        mut target_blocks: usize,
        max_block_bytes: usize,
        max_block_cycles: Cycle,
        min_fee_rate: FeeRate,
    ) -> FeeRate {
        debug_assert!(target_blocks > 0);
        let iter = self.entries.iter_by_score().rev();
        let mut current_block_bytes = 0;
        let mut current_block_cycles = 0;
        for entry in iter {
            current_block_bytes += entry.inner.size;
            current_block_cycles += entry.inner.cycles;
            if current_block_bytes >= max_block_bytes || current_block_cycles >= max_block_cycles {
                target_blocks -= 1;
                if target_blocks == 0 {
                    return entry.inner.fee_rate();
                }
                current_block_bytes = entry.inner.size;
                current_block_cycles = entry.inner.cycles;
            }
        }

        min_fee_rate
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
