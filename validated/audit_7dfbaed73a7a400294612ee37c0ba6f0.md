Audit Report

## Title
Unscaled Weight in Tx-Pool Minimum Fee Rate Check Allows Cycle-Heavy Transactions to Bypass `min_fee_rate` — (File: tx-pool/src/util.rs)

## Summary
`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee using only the serialized byte size of a transaction, ignoring the cycles dimension. The correct normalized weight — `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)` — is used in all post-admission contexts (`TxEntry::fee_rate`, sorting, eviction) but not at the admission gate. An unprivileged submitter can craft a transaction with a small serialized size but near-maximum cycles, pay a fee that satisfies the size-only check, be admitted to the pool, and have the transaction relayed to peers — all at an effective fee rate up to ~60× below `min_fee_rate`.

## Finding Description
`check_tx_fee` is called during `pre_check` before script execution, so cycles are not yet known. The function signature accepts only `tx_size: usize` and uses it directly as the weight:

```rust
// tx-pool/src/util.rs L42-45
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The code comment explicitly acknowledges the theoretical gap. The correct weight function is defined and used everywhere else:

```rust
// util/types/src/core/tx_pool.rs L298-303
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

`TxEntry::fee_rate()` uses `get_transaction_weight` correctly post-admission: [3](#0-2) 

Both call sites in `process.rs` pass only `tx_size` to `check_tx_fee`: [4](#0-3) 

After `pre_check` passes, `verify_rtx` executes scripts and returns actual cycles, but there is no second fee-rate gate using the proper weight before the transaction is admitted and relayed. The pool's eviction logic will eventually remove low-weight transactions when the pool is full, but relay to peers has already occurred by then.

## Impact Explanation
This matches the allowed bounty impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**.

With `max_tx_verify_cycles = 70,000,000` and `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`:
- Cycle-equivalent bytes at max cycles: `70,000,000 × 0.000_170_571_4 ≈ 11,940`
- Minimum realistic tx size: ~200 bytes
- Maximum weight ratio: `11,940 / 200 ≈ 60×`

An attacker can flood the mempool and trigger peer relay at ~1/60th the intended minimum cost. Because peers apply the same size-only check, the under-priced transactions propagate across the network, causing sustained congestion at a fraction of the intended economic barrier.

## Likelihood Explanation
The entry path is fully open: any caller of the `send_transaction` RPC or the P2P relay path reaches `check_tx_fee`. No special privilege, key, or majority hashpower is required. The discrepancy is deterministic and the code comment confirms the gap is real and known. The attack is repeatable indefinitely as long as the node is reachable. [5](#0-4) 

## Recommendation
After `verify_rtx` returns the actual cycles, perform a second fee-rate check using the proper weight before admitting the transaction:

```rust
let weight = get_transaction_weight(tx_size, completed.cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

Alternatively, pass a declared-cycles value (already present in the relay protocol) into `check_tx_fee` for an early gate, consistent with how `TxEntry::fee_rate()` and all other fee-rate computations work. [6](#0-5) 

## Proof of Concept
1. Construct a CKB transaction with:
   - Serialized size: ~200 bytes (minimal inputs/outputs, small lock script)
   - A lock script that loops in CKB-VM consuming ~70,000,000 cycles
   - Fee: 201 shannons

2. Submit via `send_transaction` RPC to a node with `min_fee_rate = 1000` shannons/KW.

3. At `check_tx_fee`:
   ```
   min_fee = 1000 * 200 / 1000 = 200 shannons
   fee (201) >= min_fee (200) → ADMITTED
   ```

4. After `verify_rtx` executes the script (70M cycles confirmed), the transaction enters the pool and is relayed to peers.

5. Actual effective fee rate:
   ```
   weight = max(200, 70_000_000 * 0.000_170_571_4) = max(200, 11_940) = 11_940
   effective_fee_rate = 201 * 1000 / 11_940 ≈ 16.8 shannons/KW
   ```
   This is ~98% below the configured `min_fee_rate`. Repeat to flood the mempool and propagate to peers. [7](#0-6)

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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/process.rs (L289-294)
```rust
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
```
