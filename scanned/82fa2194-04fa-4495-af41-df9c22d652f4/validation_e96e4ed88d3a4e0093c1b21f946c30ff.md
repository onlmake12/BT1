Audit Report

## Title
Tx-Pool Minimum Fee Rate Admission Check Uses Size-Only Weight, Allowing High-Cycle Transactions to Bypass the Fee Gate - (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` enforces the minimum fee rate using only the serialized transaction size, not the true resource weight (`max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`). After `pre_check` passes, `_process_tx` calls `verify_rtx` to obtain the actual cycle count and constructs a `TxEntry` with the real cycles, but never re-checks the fee rate against the weight-based minimum. Transactions with high cycle consumption are admitted to the pool with an effective weight-based fee rate far below `min_fee_rate`, enabling pool bloat and forcing expensive script verification at below-minimum cost.

## Finding Description

`check_tx_fee` in `tx-pool/src/util.rs` explicitly uses only `tx_size` for the minimum fee computation, with a comment acknowledging this is intentional as a "cheap check": [1](#0-0) 

This check runs inside `pre_check` in `tx-pool/src/process.rs`, before script verification: [2](#0-1) 

After `pre_check` passes, `_process_tx` calls `verify_rtx` to obtain the actual cycle count, then constructs a `TxEntry` with the real cycles — but performs no second fee-rate check using the weight-based formula: [3](#0-2) 

The admitted entry's true fee rate is computed using the full weight: [4](#0-3) 

Where `get_transaction_weight` is: [5](#0-4) 

With `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`: [6](#0-5) 

**Concrete example:**
- Transaction size: 1,000 bytes → passes size-based fee check with fee = 1,000 shannons (at default `min_fee_rate = 1,000 shannons/KB`)
- Script cycles: 70,000,000 (the `max_tx_verify_cycles` default)
- True weight: `max(1000, 70_000_000 × 0.000_170_571_4)` = `max(1000, 11,940)` = **11,940**
- Actual fee rate: `1000 × 1000 / 11940` ≈ **83 shannons/KB** — ~12× below the 1,000 shannons/KB minimum

The `limit_size` eviction mechanism in `tx-pool/src/pool.rs` does evict by weight-based fee rate when the pool exceeds `max_tx_pool_size`, meaning these low-fee-rate entries are evicted first when the pool fills: [7](#0-6) 

However, this does not prevent the admission of such transactions or the forced execution of expensive scripts at below-minimum cost. The attacker can continuously submit new transactions, each requiring full script verification (up to 70M cycles), while paying only the size-based minimum fee. The pool will transiently contain economically unviable transactions, and the node bears the CPU cost of verifying them.

## Impact Explanation

This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points).**

An attacker can force the node to execute maximally expensive scripts (up to `max_tx_verify_cycles`) while paying only the size-based minimum fee — a fraction of the weight-based minimum. At scale, this degrades node throughput, fills the pool with economically unviable transactions (displacing legitimate ones until eviction), and imposes CPU costs disproportionate to the fees paid. The resource accounting gap is structural: the fee gate does not reflect the true cost of admission.

## Likelihood Explanation

Reachable by any unprivileged actor via:
- The `send_transaction` JSON-RPC endpoint (local or remote RPC caller)
- The P2P relay protocol (`submit_remote_tx` path)

The attacker only needs to deploy a RISC-V script that runs a tight loop consuming close to `max_tx_verify_cycles` cycles, with a small serialized transaction body. This is straightforward on CKB given the programmable VM. No special privileges, leaked keys, or victim mistakes are required. The attack is repeatable and cheap relative to the resource cost imposed on the node.

## Recommendation

After `verify_rtx` returns the actual cycle count, perform a second fee-rate check using the true weight:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee_by_weight.as_u64(), fee.as_u64())), snapshot));
}
```

This mirrors the approach already used for pool ordering (`fee_rate()` in `entry.rs`) and fee estimation (`estimate_fee_rate` in `pool.rs`). The existing size-only check in `check_tx_fee` can remain as a fast pre-filter before script execution, with the weight-based check added post-verification in `_process_tx`.

## Proof of Concept

1. Craft a CKB transaction with:
   - One input cell consuming a lock script that runs a tight RISC-V loop consuming ~70,000,000 cycles
   - One output cell
   - Serialized size ≈ 1,000 bytes
   - Fee = 1,000 shannons (exactly `min_fee_rate × size / 1000`)

2. Submit via `send_transaction` RPC or P2P relay.

3. Observe: `check_tx_fee` passes (fee ≥ `min_fee_rate.fee(1000)` = 1,000 shannons).

4. After script verification, `verified.cycles` ≈ 70,000,000. True weight ≈ 11,940. Actual fee rate ≈ 83 shannons/KB.

5. The `TxEntry` is admitted to the pool with `fee_rate()` ≈ 83 shannons/KB, far below the 1,000 shannons/KB minimum.

6. Repeat continuously to force expensive script verification at below-minimum cost, transiently fill the pool with economically unviable transactions, and displace legitimate higher-fee-rate transactions until eviction catches up.

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

**File:** tx-pool/src/pool.rs (L292-329)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
                }
            }
        }
        self.pool_map.entries.shrink_to_fit();
        ret
    }
```
