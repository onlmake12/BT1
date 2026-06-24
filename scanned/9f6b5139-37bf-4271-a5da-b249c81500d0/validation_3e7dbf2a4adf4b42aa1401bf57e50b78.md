Audit Report

## Title
Tx-Pool Minimum Fee Rate Gate Uses Serialized Size Instead of Weight, Allowing High-Cycle Transactions to Bypass the Pre-Check and Force Expensive Script Verification - (File: tx-pool/src/util.rs)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces `min_fee_rate` using only the transaction's serialized byte size, while the rest of the pool uses the correct weight metric (`max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`). An attacker can craft a transaction with a small serialized size but near-maximum cycle consumption, pay a fee just above `min_fee_rate * tx_size`, pass the cheap pre-check gate, and force the node to perform full, expensive script verification. The code itself acknowledges the discrepancy with a comment but treats it as an acceptable approximation.

## Finding Description

In `check_tx_fee` (`tx-pool/src/util.rs` L42-45), the minimum fee is computed using raw serialized size:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The correct weight formula used everywhere else in the pool (`util/types/src/core/tx_pool.rs` L298-303) is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

`check_tx_fee` is called in `pre_check` **before** script execution, so cycles are not yet known: [3](#0-2) 

After verification, the entry is created with actual cycles and admitted to the pool: [4](#0-3) 

The pool's eviction and scoring correctly use `get_transaction_weight(self.size, self.cycles)`: [5](#0-4) 

The verify queue's fullness check is also based on serialized tx size, not weight: [6](#0-5) 

This means the verify queue can be filled with many small-but-high-cycle transactions (256MB / 200 bytes = up to 1.28M entries), each requiring expensive verification.

Critically, there is **no check against `recent_reject` during submission**. If a transaction fails script verification, the UTXO is not consumed and the transaction is not blocked from resubmission (only the pool/queue duplicate check applies, and once the tx is rejected and removed from the queue, it can be resubmitted). The attacker can resubmit the same or slightly varied transactions repeatedly.

## Impact Explanation

This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

With default parameters (`min_fee_rate = 1000` shannons/KB, `max_tx_verify_cycles = 70,000,000`):
- Maximum cycle-derived weight: `70,000,000 × 0.000_170_571_4 ≈ 11,940` bytes
- A 200-byte transaction with 70M cycles has actual weight 11,940 bytes
- Correct minimum fee: `1000 × 11,940 / 1000 = 11,940` shannons
- Fee paid to pass `check_tx_fee`: `1000 × 200 / 1000 = 200` shannons (~60× underpayment)

The attacker forces full script verification (up to 70M VM cycles of CPU) per transaction while paying only ~1/60th of the intended minimum fee. Since the UTXO is not consumed on script failure, the attack can be sustained with minimal on-chain cost. [7](#0-6) [8](#0-7) 

## Likelihood Explanation

The attack is reachable by any unprivileged actor via the `send_transaction` RPC or P2P relay protocol. The attacker needs valid UTXOs to construct transactions, but since transactions that fail script verification do not consume UTXOs, a single UTXO suffices for a sustained attack. The discrepancy between size-based and weight-based fee rate is up to ~60× under default consensus parameters, making the bypass straightforward to engineer. The verify queue size limit is based on serialized tx size (not weight), so small transactions can saturate it with many high-cycle entries. [9](#0-8) 

## Recommendation

Compute the minimum fee in `check_tx_fee` using a conservative upper-bound weight. Since cycles are not yet known at pre-check time, use `get_transaction_weight(tx_size, max_tx_verify_cycles)`:

```rust
use ckb_types::core::tx_pool::get_transaction_weight;
let max_possible_weight = get_transaction_weight(tx_size, tx_pool.config.max_tx_verify_cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(max_possible_weight);
```

Alternatively, add a secondary fee rate check after verification (when actual cycles are known) and reject the transaction if its effective fee rate is below `min_fee_rate`. The verify queue fullness check should also account for weight rather than raw tx size to prevent queue saturation by high-cycle small transactions. [10](#0-9) 

## Proof of Concept

1. Obtain a UTXO with capacity C.
2. Construct a transaction with:
   - One input spending that UTXO
   - One output with capacity `C - 200` shannons (fee = 200 shannons)
   - A lock script that consumes ~70,000,000 cycles and then **fails** (so the UTXO is not consumed)
   - Serialized size ~200 bytes (script code lives in a cell dep, not in the tx itself)
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200` shannons; the transaction passes.
5. The node performs full script verification consuming ~70M cycles.
6. The script fails; the transaction is rejected; the UTXO remains unspent.
7. Craft a slightly different transaction (e.g., different witness bytes) spending the same UTXO to get a different txid, bypassing the `check_txid_collision` guard.
8. Repeat steps 3–7 continuously to exhaust node CPU with a single UTXO. [11](#0-10) [12](#0-11)

### Citations

**File:** tx-pool/src/util.rs (L20-26)
```rust
pub(crate) fn check_txid_collision(tx_pool: &TxPool, tx: &TransactionView) -> Result<(), Reject> {
    let short_id = tx.proposal_short_id();
    if tx_pool.contains_proposal_id(&short_id) {
        return Err(Reject::Duplicated(tx.hash()));
    }
    Ok(())
}
```

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

**File:** util/types/src/core/tx_pool.rs (L279-279)
```rust
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

**File:** tx-pool/src/component/verify_queue.rs (L17-18)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```

**File:** tx-pool/src/component/verify_queue.rs (L104-106)
```rust
    pub fn is_full(&self, add_tx_size: usize) -> bool {
        add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
    }
```

**File:** util/app-config/src/configs/tx_pool.rs (L20-21)
```rust
    /// tx pool rejects txs that cycles greater than max_tx_verify_cycles
    pub max_tx_verify_cycles: Cycle,
```
