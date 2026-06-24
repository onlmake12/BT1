The code confirms all claims. Let me verify the key facts:

1. `check_tx_fee` uses only `tx_size` with an explicit "cheap check" comment [1](#0-0) 
2. `pre_check` calls `check_tx_fee` before script execution [2](#0-1) 
3. After `verify_rtx` returns `verified.cycles`, `TxEntry` is created at L751 with no second fee-rate check [3](#0-2) 
4. `get_transaction_weight` uses `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)` [4](#0-3) 
5. `fee_rate()`, `AncestorsScoreSortKey`, and `EvictKey` all use weight-based calculation post-admission [5](#0-4) 

---

Audit Report

## Title
Min-Fee-Rate Admission Check Uses Only Serialized Size, Ignoring Cycle-Based Weight — Cycle-Heavy Transactions Bypass Effective Fee Floor - (File: `tx-pool/src/util.rs`)

## Summary
`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size, while the correct fee-rate metric used everywhere else in the codebase is `weight = max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. Because cycles are unavailable before script execution and no post-verification fee-rate re-check is performed, a transaction with a small serialized size but near-maximum cycle consumption passes the admission gate at a fraction of the true minimum cost, enabling sustained network-wide verification resource waste at ~1/20th the intended cost.

## Finding Description
**Admission gate (`tx-pool/src/util.rs`, L42–52):**
```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```
`check_tx_fee` is called inside `pre_check` (`process.rs` L289 and L294), which runs before `verify_rtx` (L724–732). At that point, cycles are not yet known, so only `tx_size` is used.

After `verify_rtx` returns `verified.cycles`, the code at `process.rs` L751 creates `TxEntry::new(rtx, verified.cycles, fee, tx_size)` and proceeds directly to `submit_entry`. There is no second fee-rate check using the actual cycles.

**Correct weight formula (`util/types/src/core/tx_pool.rs`, L298–303):**
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```
This formula is used by `TxEntry::fee_rate()` (entry.rs L114–118), `AncestorsScoreSortKey` (entry.rs L221–231), and `EvictKey` (entry.rs L234–247) — everywhere after admission. The admission gate and all post-admission scheduling use fundamentally different fee-rate metrics, creating a structural inconsistency.

## Impact Explanation
**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

With default config (`min_fee_rate = 1000 shannons/KW`, `max_tx_verify_cycles = 70,000,000`):
- `tx_size = 597` bytes, `cycles = 70,000,000`
- Size-based `min_fee = 597` shannons → admitted
- Correct `weight = max(597, 70,000,000 × 0.000_170_571_4) = 11,940`
- Weight-based `min_fee = 11,940` shannons
- Effective fee rate ≈ 50 shannons/KW — **20× below the configured floor**

Each admitted transaction forces the local node to execute 70M cycles of script verification, then relays to peers who each also execute 70M cycles. An attacker sustains this at 1/20th the intended cost, causing amplified verification resource exhaustion across the network.

## Likelihood Explanation
Any unprivileged user can submit transactions via the `send_transaction` RPC. Crafting a transaction with a computationally expensive lock script (~70M cycles) and a small serialized body requires only the attacker's own UTXOs and no special privileges. The attack is repeatable as long as the attacker controls UTXOs, and each iteration costs only ~597 shannons instead of ~11,940 shannons. The 20× cost reduction makes sustained pool-pollution and peer-relay amplification economically viable.

## Recommendation
After `verify_rtx` returns and actual cycles are known, perform a second fee-rate check using the correct weight before creating the `TxEntry` (in `_process_tx`, `process.rs` around L751):

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((
        Err(Reject::LowFeeRate(
            tx_pool_config.min_fee_rate,
            min_fee_by_weight.as_u64(),
            fee.as_u64(),
        )),
        snapshot,
    ));
}
```
This mirrors how `TxEntry::fee_rate()` and `AncestorsScoreSortKey` already compute the effective fee rate and closes the gap between the admission gate and the actual scheduling metric.

## Proof of Concept
1. Deploy a lock script that loops to consume ~70,000,000 cycles.
2. Craft a transaction spending a UTXO locked by that script, with serialized size ~597 bytes.
3. Set fee = 597 shannons (exactly `min_fee_rate.fee(597)`).
4. Submit via `send_transaction` RPC.
5. `check_tx_fee` computes `min_fee = 1000 × 597 / 1000 = 597 shannons`. Fee equals min_fee → admitted.
6. `verify_rtx` executes 70M cycles of script work on the local node.
7. Transaction is relayed to peers; each peer also executes 70M cycles.
8. Actual weight = `max(597, 70,000,000 × 0.000_170_571_4) = 11,940`. Effective fee rate ≈ 50 shannons/KW — 20× below the configured floor.
9. Transaction sits at the bottom of the priority queue and is eventually evicted, but not before consuming verification resources on the local node and all relay peers.
10. Repeat with fresh UTXOs to sustain the attack at 1/20th the intended cost.

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

**File:** tx-pool/src/process.rs (L289-294)
```rust
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
```

**File:** tx-pool/src/process.rs (L751-753)
```rust
        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);

        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
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
