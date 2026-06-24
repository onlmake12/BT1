Audit Report

## Title
Fee Rate Minimum Check Uses Serialized Size Instead of Transaction Weight, Allowing High-Cycle Transactions to Underpay — (File: tx-pool/src/util.rs)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee using only the serialized transaction size (`tx_size`) as the weight argument to `FeeRate::fee()`. However, `FeeRate` is defined as shannons per kilo-weight, where weight is `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For transactions where cycles dominate, the actual weight is substantially larger than `tx_size`, making the minimum fee check far too lenient. No compensating weight-based check is performed after `verify_rtx` determines actual cycles, so high-cycle transactions are admitted to the pool at a fraction of the intended minimum fee rate.

## Finding Description

`FeeRate` is explicitly documented and implemented as shannons per kilo-weight: [1](#0-0) [2](#0-1) 

Transaction weight is defined as: [3](#0-2) 

`check_tx_fee` passes raw `tx_size` — not the actual weight — to `FeeRate::fee()`. The code comment acknowledges this is an approximation: [4](#0-3) 

In `_process_tx`, `pre_check` (which calls `check_tx_fee`) runs before `verify_rtx`. After `verify_rtx` returns `verified.cycles`, there is no second fee rate check using `get_transaction_weight(tx_size, verified.cycles)`. The code proceeds directly to `TxEntry::new` and `submit_entry`: [5](#0-4) 

This means the size-only check is the **only** admission gate for fee rate policy. For a transaction with `tx_size = 300` bytes and `cycles = 70,000,000`:
- Actual weight = `max(300, 70_000_000 × 0.000_170_571_4)` ≈ 11,940
- `check_tx_fee` computes: `min_fee = 1000 × 300 / 1000 = 300 shannons`
- Correct minimum: `1000 × 11,940 / 1000 = 11,940 shannons`
- Attacker pays ~49× less than the policy intends

## Impact Explanation

This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** An attacker can continuously submit valid high-cycle, small-size transactions at ~2% of the intended minimum fee rate, flooding the tx-pool's verify queue with CPU-intensive script executions. The pool's `max_tx_verify_cycles` (default 70,000,000) is the only upper bound on per-transaction CPU cost, and the fee rate policy — the economic deterrent against such abuse — is effectively bypassed for this class of transactions.

## Likelihood Explanation

Any unprivileged caller with access to `send_transaction` RPC can trigger this. Crafting a valid transaction with high cycles requires only authoring a lock script that executes a tight loop (a standard RISC-V program). No privileged access, leaked keys, or majority hashpower is required. The attack is repeatable and cheap: each submission costs only `min_fee_rate × tx_size / 1000` shannons rather than `min_fee_rate × weight / 1000` shannons. [6](#0-5) 

## Recommendation

After `verify_rtx` returns actual cycles in `_process_tx`, perform a second fee rate check using the true transaction weight:

```rust
// After verify_rtx returns verified.cycles:
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(
        tx_pool.config.min_fee_rate,
        min_fee_by_weight.as_u64(),
        fee.as_u64(),
    )), snapshot));
}
```

The pre-check in `check_tx_fee` can remain as a fast early-rejection gate (rejecting transactions that cannot even meet the size-based floor), but it must be followed by a weight-based check once actual cycles are known.

## Proof of Concept

1. Author a CKB lock script that executes a tight loop consuming ~70,000,000 cycles. Compile to RISC-V and embed in a transaction with ~300 bytes serialized size.
2. Compute fee: `min_fee_rate (1000) × 300 / 1000 = 300 shannons`. Set transaction fee to exactly 300 shannons.
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` passes: `300 >= 1000 × 300 / 1000 = 300`. ✓
5. `verify_rtx` executes the script, consuming 70,000,000 cycles. No post-verification fee rate check is performed.
6. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` is created and submitted. [7](#0-6) 
7. Actual weight = `max(300, 11,940)` = 11,940. Effective fee rate = `300 × 1000 / 11,940 ≈ 25 shannons/KW` — ~49× below the configured 1,000 shannons/KW minimum.
8. Repeat to flood the pool with high-cycle transactions at a fraction of the intended cost, exhausting CPU and pool memory.

### Citations

**File:** util/types/src/core/fee_rate.rs (L3-7)
```rust
/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);

const KW: u64 = 1000;
```

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
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

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
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

**File:** tx-pool/src/process.rs (L715-753)
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
```
