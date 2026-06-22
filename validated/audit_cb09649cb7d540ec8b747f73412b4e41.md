### Title
Tx-Pool Minimum Fee Rate Admission Check Uses Size-Only Weight, Allowing High-Cycle Transactions to Bypass the Fee Gate - (File: `tx-pool/src/util.rs`)

---

### Summary

The tx-pool admission check in `check_tx_fee` enforces the minimum fee rate using only the serialized transaction **size** as the weight denominator. However, the actual resource cost of a transaction is `weight = max(size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For transactions with scripts that consume many cycles, the true weight can be an order of magnitude larger than the size alone. There is no second fee-rate check after script verification reveals the actual cycle count. As a result, any unprivileged submitter can craft a transaction that passes the fee gate but has an effective weight-based fee rate far below `min_fee_rate`, causing pool bloat and miner revenue loss with no correction mechanism.

---

### Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` computes the minimum required fee using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

This check runs inside `pre_check`, **before** script verification, because cycles are not yet known: [2](#0-1) 

After `pre_check` passes, `_process_tx` calls `verify_rtx` to obtain the actual cycle count, then creates a `TxEntry` with the real cycles — but **never re-checks the fee rate against the weight-based minimum**: [3](#0-2) 

Once admitted, the entry's true fee rate is computed using the full weight:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [4](#0-3) 

Where `get_transaction_weight` is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [5](#0-4) 

With `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`: [6](#0-5) 

**Concrete example:**
- Transaction size: 1,000 bytes → passes size-based fee check with fee = 1,000 shannons (at default `min_fee_rate = 1,000 shannons/KB`)
- Script cycles: 70,000,000 (the `max_tx_verify_cycles` default)
- True weight: `max(1000, 70_000_000 × 0.000_170_571_4)` = `max(1000, 11,940)` = **11,940**
- Actual fee rate: `1000 × 1000 / 11940` ≈ **83 shannons/KB** — ~12× below the 1,000 shannons/KB minimum

The transaction is admitted to the pool with a fee rate far below `min_fee_rate`. There is no eviction or correction step that re-checks the weight-based fee rate after cycles are known. [7](#0-6) 

---

### Impact Explanation

An unprivileged attacker can flood the tx-pool with transactions that:
1. Pass the size-based fee gate with minimal fees
2. Consume maximum script execution cycles (up to `max_tx_verify_cycles`)
3. Have an effective weight-based fee rate far below `min_fee_rate`
4. Occupy pool space (up to `max_tx_pool_size = 180 MB`) and displace legitimate higher-fee-rate transactions

Miners selecting transactions by weight-based fee rate (as done in `tx_selector` and `estimate_fee_rate`) will deprioritize or never include these transactions. The pool fills with economically unviable transactions, degrading throughput and causing legitimate transactions to be evicted or delayed. This is a direct resource-accounting leak: the fee paid does not correspond to the true resource cost, and there is no correction mechanism.

---

### Likelihood Explanation

This is reachable by any unprivileged actor via:
- The `send_transaction` JSON-RPC endpoint (local or remote RPC caller)
- The P2P relay protocol (`submit_remote_tx` path), where a peer relays a transaction with a declared cycle count

The attacker only needs to deploy a script that consumes many cycles (e.g., a tight loop up to `max_tx_verify_cycles`) with a small serialized transaction body. This is straightforward on CKB given the programmable RISC-V VM. The discrepancy between size-based and weight-based fee rates is maximized when cycles dominate, which is common for non-trivial smart contract transactions.

---

### Recommendation

After `verify_rtx` returns the actual cycle count, perform a second fee-rate check using the true weight:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

This mirrors the approach already used for pool ordering and fee estimation, and closes the gap between the admission gate and the actual resource cost. The existing size-only check can remain as a fast pre-filter before script execution, with the weight-based check added post-verification.

---

### Proof of Concept

1. Craft a CKB transaction with:
   - One input cell consuming a lock script that runs a tight RISC-V loop consuming ~70,000,000 cycles
   - One output cell
   - Serialized size ≈ 1,000 bytes
   - Fee = 1,000 shannons (exactly `min_fee_rate × size / 1000`)

2. Submit via `send_transaction` RPC or P2P relay.

3. Observe: `check_tx_fee` passes (fee ≥ `min_fee_rate.fee(1000)` = 1,000 shannons).

4. After script verification, `verified.cycles` ≈ 70,000,000. True weight ≈ 11,940. Actual fee rate ≈ 83 shannons/KB.

5. The `TxEntry` is admitted to the pool with `fee_rate()` ≈ 83 shannons/KB, far below the 1,000 shannons/KB minimum.

6. Repeat to fill the pool with such entries, evicting legitimate transactions and degrading miner block assembly quality.

### Citations

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

**File:** tx-pool/src/process.rs (L715-754)
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

        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
        try_or_return_with_snapshot!(ret, submit_snapshot);
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
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

**File:** util/app-config/src/legacy/tx_pool.rs (L9-15)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
// default max tx verify cycles
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
// default max ancestors count
```
