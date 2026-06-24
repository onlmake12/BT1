Looking at the actual code to validate each claim in the report.

Audit Report

## Title
Fee Rate Minimum Check Uses Serialized Size Instead of Transaction Weight, Allowing High-Cycle Transactions to Underpay - (File: tx-pool/src/util.rs)

## Summary

`check_tx_fee` computes the minimum required fee using raw serialized `tx_size` as the weight argument to `FeeRate::fee()`, but `FeeRate` is defined as shannons per kilo-weight where weight is `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. After `verify_rtx` returns actual cycles, no second weight-based fee check is performed. Any unprivileged submitter can craft a valid high-cycle, small-size transaction and pay only a fraction of the intended minimum fee rate.

## Finding Description

`FeeRate` is documented and implemented as shannons per kilo-weight: [1](#0-0) [2](#0-1) 

The correct weight formula accounts for cycles: [3](#0-2) 

But `check_tx_fee` passes raw `tx_size` — not weight — to `FeeRate::fee()`. The code comment explicitly acknowledges this is an approximation: [4](#0-3) 

In `_process_tx`, the flow is:
1. `pre_check` → `check_tx_fee` (size-only check, before cycles are known)
2. `verify_rtx` → returns `verified.cycles` (actual cycles now known)
3. **No second fee rate check using `get_transaction_weight(tx_size, verified.cycles)`**
4. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` → submitted to pool [5](#0-4) 

After `verify_rtx` returns actual cycles at line 734, `verified.cycles` is available but is never used to recompute the minimum fee. The transaction is admitted directly.

## Impact Explanation

This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" — High (10001–15000 points).**

With default config (`min_fee_rate = 1000 shannons/KW`, `max_tx_verify_cycles = 70,000,000`):
- A transaction with ~300 bytes serialized size but 70,000,000 cycles has actual weight ≈ `max(300, 70_000_000 × 0.000_170_571_4)` = **11,940**.
- `check_tx_fee` requires only `1000 × 300 / 1000 = 300 shannons`.
- Correct minimum should be `1000 × 11,940 / 1000 = 11,940 shannons`.
- Attacker pays **~49× less** than the intended minimum.

An attacker can repeatedly flood the tx-pool with high-cycle transactions at a fraction of the intended cost, consuming CPU during script verification and pool memory, causing network congestion.

## Likelihood Explanation

Any unprivileged caller with access to `send_transaction` RPC can trigger this. Crafting a valid transaction with high cycles requires only authoring a lock script with a tight loop (a standard RISC-V program). No privileged access, leaked keys, or majority hashpower is required. The attack is repeatable and cheap relative to the intended cost. [6](#0-5) 

## Recommendation

After `verify_rtx` returns actual cycles in `_process_tx`, perform a second fee rate check using the true transaction weight:

```rust
// After verify_rtx returns verified.cycles:
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

The existing size-only check in `check_tx_fee` can remain as a cheap pre-filter, but a definitive weight-based check must follow script execution once actual cycles are known. [7](#0-6) 

## Proof of Concept

1. Author a CKB lock script that executes a tight loop consuming ~70,000,000 cycles. Compile to RISC-V and embed in a transaction with ~300 bytes serialized size.
2. Compute fee: `min_fee_rate (1000) × 300 / 1000 = 300 shannons`. Set transaction fee to exactly 300 shannons.
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` passes: `300 >= 1000 × 300 / 1000 = 300`. ✓
5. `verify_rtx` executes the script consuming 70,000,000 cycles. No second fee check is performed.
6. Actual weight = `max(300, 11,940)` = 11,940. Effective fee rate = `300 × 1000 / 11,940 ≈ 25 shannons/KW` — ~49× below the configured 1,000 shannons/KW minimum.
7. Transaction is admitted. Repeat to flood the pool with high-cycle transactions at ~49× below intended minimum cost. [8](#0-7) [9](#0-8)

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
