Audit Report

## Title
`check_tx_fee` Admission Gate Uses Serialized Size Only, Ignoring Cycles-Based Weight — Allows High-Cycle Transactions to Bypass Intended Fee Floor - (File: `tx-pool/src/util.rs`)

## Summary
`check_tx_fee` enforces the minimum fee rate using only the transaction's serialized byte size, while the correct weight is `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. A transaction with small serialized size but near-maximum cycles passes the admission gate while paying up to ~60× less than the fee `min_fee_rate` intends to require. No second fee check is performed after `verify_rtx` returns the actual cycles, leaving the gap permanently open.

## Finding Description
In `tx-pool/src/util.rs` L45, `check_tx_fee` computes the minimum fee using only `tx_size`:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code comment at L42–44 explicitly acknowledges the gap: *"Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly."* [1](#0-0) 

The correct weight function in `util/types/src/core/tx_pool.rs` L298–303 is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
``` [2](#0-1) 

In `_process_tx` (`tx-pool/src/process.rs` L705–777), the flow is:

1. `pre_check` → `check_tx_fee` (size-only gate, L715–717)
2. `verify_rtx` returns actual cycles (L724–734)
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` is created (L751)
4. **No second fee check is performed** against the actual weight before `submit_entry` (L753) [3](#0-2) 

After admission, `TxEntry::fee_rate()` (`tx-pool/src/component/entry.rs` L115–118) correctly uses `get_transaction_weight(self.size, self.cycles)`, creating a permanent split: the admission gate uses size-only weight, while all post-admission logic uses the full weight including cycles. [4](#0-3) 

For remote transactions, `declared_cycles` is only used to cap `max_cycles` for verification and to reject mismatches (`DeclaredWrongCycles`); it is never fed back into a weight-aware fee check. [5](#0-4) 

## Impact Explanation
This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

With default config (`min_fee_rate = 1000 shannons/KW`, `max_tx_verify_cycles = 70,000,000`):

| Metric | Value |
|---|---|
| Attacker tx serialized size | 200 bytes |
| Attacker tx cycles | 70,000,000 |
| Actual weight | `max(200, 70M × 0.000_170_571_4)` = **11,940** |
| Fee required by gate | `1000 × 200 / 1000` = **200 shannons** |
| Fee that should be required | `1000 × 11,940 / 1000` = **11,940 shannons** |
| Discount factor | **~60×** |

An attacker can flood the tx-pool with high-cycle, low-size transactions at ~60× below the intended fee floor. Each admitted transaction is relayed to all peers via the P2P path, causing each peer to spend ~70M cycles of CPU on script verification. With many UTXOs, this constitutes sustained network congestion at a fraction of the intended cost.

## Likelihood Explanation
High. The exploit requires no privilege. Both entry paths are open to any external actor:

- **RPC path**: `send_transaction` → `submit_local_tx` → `process_tx` → `pre_check` → `check_tx_fee`
- **P2P relay path**: `submit_remote_tx` → `process_tx` → `pre_check` → `check_tx_fee`

Crafting the attack requires only deploying a lock script with a tight RISC-V loop (a standard CKB operation), then creating minimal transactions spending cells locked by it. The script can be deployed once and reused across arbitrarily many attack transactions. The code comment itself confirms the developers were aware of the theoretical gap.

## Recommendation
After `verify_rtx` returns the actual cycles in `_process_tx`, perform a second fee check using the real weight before calling `submit_entry`:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(actual_weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(
        tx_pool_config.min_fee_rate, min_fee_by_weight.as_u64(), fee.as_u64()
    )), snapshot));
}
```

This closes the gap entirely without requiring cycles to be known at `pre_check` time. The existing size-only check in `check_tx_fee` can be retained as a cheap early filter.

## Proof of Concept
1. Deploy a lock script on a local CKB node that executes a tight RISC-V loop consuming ~69,000,000 cycles.
2. Create a cell locked by this script with capacity 100 CKB.
3. Construct an attack transaction: 1 input (the cell), 1 output (same capacity minus 200 shannons fee). Serialized size ≈ 200 bytes.
4. Submit via `send_transaction` RPC.
5. **Expected**: `check_tx_fee` passes (200 shannons ≥ `1000 × 200 / 1000 = 200`). `verify_rtx` runs and returns ~69M cycles. No second fee check occurs. Transaction is admitted and relayed.
6. **Actual fee rate**: `200 × 1000 / 11,769 ≈ 17 shannons/KW` — far below `min_fee_rate = 1000 shannons/KW`.
7. Repeat with many UTXOs to flood the pool and relay network. Each peer expends ~69M cycles of CPU per transaction at a cost to the attacker of 200 shannons instead of the intended ~11,769 shannons.

### Citations

**File:** tx-pool/src/util.rs (L42-53)
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
    Ok(fee)
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
