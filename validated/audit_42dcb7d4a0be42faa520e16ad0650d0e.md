Audit Report

## Title
`check_tx_fee` Uses Serialized Size Only as Fee Weight, No Post-Verification Composite Weight Check — (`tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate gate using only the serialized byte size (`tx_size`) as the weight denominator. After `verify_rtx` returns actual cycles in `_process_tx`, no second fee rate check is performed using the correct composite weight `get_transaction_weight(tx_size, cycles)`. An attacker can craft a CPU-heavy transaction with a small serialized body, pay a fee just above the size-only minimum, pass the gate, force full script verification, and be admitted to the tx-pool at a fraction of the correct minimum fee cost.

## Finding Description

**Root cause:** `check_tx_fee` at `tx-pool/src/util.rs:45` computes `min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64)`. The code comment at lines 42–44 explicitly acknowledges this is a deliberate approximation: *"Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly."*

The correct composite weight formula is defined in `util/types/src/core/tx_pool.rs:298–303`:
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```
where `DEFAULT_BYTES_PER_CYCLES ≈ 0.000_170_571_4`.

**Missing post-verification check:** In `_process_tx` (`tx-pool/src/process.rs`):
- Line 715: `pre_check` → calls `check_tx_fee` with `tx_size` only (line 289)
- Lines 724–732: `verify_rtx` → returns actual `verified.cycles`
- Lines 736–748: `DeclaredWrongCycles` check — only validates declared vs. actual cycles, not fee adequacy
- Line 751: `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — entry created and submitted

There is no step between the `verify_rtx` return and `TxEntry::new` that re-evaluates the fee rate using the now-known `verified.cycles`. `TxEntry::fee_rate()` at `tx-pool/src/component/entry.rs:115–118` does use `get_transaction_weight(self.size, self.cycles)` correctly, but this is only used for sorting and eviction priority — not for admission gating.

**Exploit flow:**
- Attacker crafts a transaction: serialized size ≈ 200 bytes, cycles ≈ 70,000,000
- At `min_fee_rate = 1000 shannons/KB`:
  - Size-only `min_fee = 1000 * 200 / 1000 = 200 shannons` → attacker pays 201 shannons → passes `check_tx_fee`
  - Proper weight = `max(200, 70,000,000 × 0.000_170_571_4) ≈ 11,940`
  - Proper `min_fee = 11,940 shannons` → attacker underpays by ~59×
- `verify_rtx` runs full 70M-cycle script verification — CPU cost already paid by the node
- `DeclaredWrongCycles` check passes (attacker knows their own script's cycle count)
- Transaction admitted to tx-pool

**Existing guards are insufficient:**
- The `DeclaredWrongCycles` check (lines 736–748) only enforces cycle declaration accuracy, not fee adequacy. For local RPC (`send_transaction`), `declared_cycles` is `None` (line 414: `remote.map(|r| r.0)`), so this check is skipped entirely.
- Pool eviction (`limit_size`) uses proper weight-based fee rate but only triggers after admission and only when the pool is full.
- The tx-pool size limit does not prevent the CPU cost of verification from being incurred.

## Impact Explanation

**High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker can repeatedly submit CPU-heavy transactions at ~59× below the correct minimum fee, forcing each target node to execute full script verification (up to `max_tx_verify_cycles = 70,000,000` cycles per transaction) before the transaction is admitted. At scale, this exhausts node CPU resources and fills the tx-pool with sub-minimum-fee-rate entries, displacing legitimate transactions and degrading relay performance across the network.

## Likelihood Explanation

Any unprivileged RPC caller (`send_transaction`) or P2P relay peer can trigger this. No special privileges are required. Crafting a script that consumes a predictable number of cycles but serializes to a small witness is straightforward — a tight loop in CKB-VM is sufficient. The attacker knows the exact cycle count because they authored the script, so the `DeclaredWrongCycles` check is trivially satisfied. The attack is repeatable: the attacker pre-creates many UTXOs locked by CPU-heavy scripts in a single setup transaction, then spends them one by one.

## Recommendation

After `verify_rtx` returns `verified.cycles` and before `submit_entry`, perform a second fee rate check using the proper composite weight in `_process_tx` (`tx-pool/src/process.rs`, after line 748):

```rust
let proper_weight = get_transaction_weight(tx_size, verified.cycles);
let proper_min_fee = tx_pool_config.min_fee_rate.fee(proper_weight);
if fee < proper_min_fee {
    return Some((
        Err(Reject::LowFeeRate(tx_pool_config.min_fee_rate, proper_min_fee.as_u64(), fee.as_u64())),
        snapshot,
    ));
}
```

This mirrors the correct pattern already used in `TxEntry::fee_rate()` (`tx-pool/src/component/entry.rs:115–118`). The pre-verification size-only check can remain as a cheap early filter; the post-verification check closes the admission gap.

## Proof of Concept

1. Configure a CKB node with default `min_fee_rate = 1000 shannons/KB`.
2. Write a CKB lock script that executes a tight loop consuming exactly 70,000,000 cycles but whose witness serializes to ≈200 bytes.
3. Create a cell locked by this script on a dev chain.
4. Submit a transaction spending that cell via `send_transaction` RPC, paying a fee of 201 shannons (above `min_fee_rate.fee(200) = 200`).
5. Observe: `check_tx_fee` passes (201 > 200); `verify_rtx` runs full 70M-cycle verification; transaction is admitted to the tx-pool.
6. Verify the admitted entry's effective fee rate: `201 * 1000 / 11940 ≈ 16 shannons/KB` — far below the 1000 shannons/KB minimum.
7. Repeat in a loop (using pre-created UTXOs) to exhaust node CPU and fill the tx-pool with sub-minimum-fee-rate entries.

---

**Code references:**

`check_tx_fee` size-only fee check: [1](#0-0) 

`get_transaction_weight` composite weight formula: [2](#0-1) 

`_process_tx` flow — no post-verification fee check between `verify_rtx` and `TxEntry::new`: [3](#0-2) 

`TxEntry::fee_rate()` uses correct composite weight (admission-only, not gating): [4](#0-3)

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

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```
