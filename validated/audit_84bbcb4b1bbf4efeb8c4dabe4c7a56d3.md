Audit Report

## Title
Min Fee Rate Admission Check Uses Serialized Size While Post-Verification Entry Uses True Weight — (`tx-pool/src/util.rs`)

## Summary
`check_tx_fee` validates the minimum fee rate using only the transaction's serialized byte size (`tx_size`) before script execution, when cycles are not yet known. After `verify_rtx` returns the actual cycle count, a `TxEntry` is created with the real cycles, but no second fee-rate check is performed against the true weight (`max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`). For a cycle-heavy, size-small transaction, the effective fee rate stored in the pool can be far below the configured `min_fee_rate`, allowing an unprivileged attacker to flood the tx pool at a fraction of the intended minimum cost.

## Finding Description
In `tx-pool/src/util.rs` lines 42–45, the code itself acknowledges the limitation with a comment:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The comment confirms this is intentionally a pre-verification cheap check. The problem is that no compensating check is performed after cycles are known. In `_process_tx` (`tx-pool/src/process.rs`), the flow is:

1. `pre_check` → `check_tx_fee` (size-only, before verification) — line 715–717
2. `verify_rtx` returns actual `verified.cycles` — line 724–734
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` is created with real cycles — line 751
4. `submit_entry` is called — **no weight-based fee re-check anywhere** [2](#0-1) 

Every downstream metric uses the true weight via `get_transaction_weight(tx_size, cycles)`:
- `fee_rate()` on `TxEntry` — `entry.rs` line 116
- `AncestorsScoreSortKey` (block assembly priority) — `entry.rs` line 223
- `EvictKey` (eviction ordering) — `entry.rs` line 236 [3](#0-2) [4](#0-3) 

A transaction with `tx_size = 1,000 bytes` and `cycles = 70,000,000` has `weight ≈ 11,940 bytes`. At `min_fee_rate = 1,000 shannons/KW`, the admission check requires only 1,000 shannons, while the true weight-based minimum would be 11,940 shannons. The transaction enters the pool with an effective fee rate of ~83 shannons/KW — roughly 12× below the configured minimum.

## Impact Explanation
An attacker can continuously submit cycle-heavy, size-small transactions paying only `min_fee_rate × tx_size` instead of `min_fee_rate × weight`, at approximately 1/12th the intended cost in the example above. While the pool's eviction mechanism (`EvictKey`) would preferentially remove these low-weight-fee-rate entries when the pool is full, the attacker can sustain the attack by continuously resubmitting, keeping the pool filled with underpriced transactions and degrading block-assembly quality for legitimate users. This maps to: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation
The attack requires no special privileges. Any peer or RPC caller can invoke `send_transaction`. Constructing a transaction with high cycle consumption and minimal serialized size (e.g., a lock script with a tight loop and minimal witness/output data) is straightforward. The attack is repeatable and cheap to sustain at scale.

## Recommendation
After `verify_rtx` returns the actual cycle count in `_process_tx`, perform a second fee-rate check using the true weight before creating the `TxEntry`:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

This requires passing `min_fee_rate` config into `_process_tx` or performing the check inside `submit_entry` where the pool config is accessible. The existing size-only check in `check_tx_fee` can remain as the cheap pre-filter it is designed to be.

## Proof of Concept
1. Construct a CKB transaction whose lock/type script executes a tight loop consuming ~70,000,000 cycles, with minimal outputs and witness (~1,000 serialized bytes).
2. Set the fee to exactly `min_fee_rate × tx_size` (e.g., 1,000 shannons at the default 1,000 shannons/KW rate).
3. Submit via `send_transaction` RPC.
4. Observe the transaction is accepted (`check_tx_fee` passes on size alone).
5. Query `get_pool_transaction` or inspect `TxEntry::fee_rate()` — the reported fee rate will be `fee / weight ≪ min_fee_rate` (≈83 shannons/KW vs. the configured 1,000 shannons/KW).
6. Repeat in a loop to sustain pool pressure at ~1/12th the intended minimum cost.

### Citations

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

**File:** tx-pool/src/process.rs (L724-751)
```rust
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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
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
