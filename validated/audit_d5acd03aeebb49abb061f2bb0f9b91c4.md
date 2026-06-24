All code references check out. Here is the validation result:

Audit Report

## Title
`check_tx_fee` Enforces Minimum Fee Against Byte Size Only, Not Cycle-Adjusted Weight — (`tx-pool/src/util.rs`)

## Summary
`check_tx_fee` computes the minimum required fee using only the transaction's serialized byte size, while the actual fee rate used for pool sorting and eviction is computed against `get_transaction_weight(size, cycles)` — the maximum of byte size and cycle-adjusted size. An attacker can craft a cycle-heavy, byte-small transaction that passes the admission check while paying a fee rate far below `min_fee_rate`, causing the node to perform expensive script verification without adequate compensation. The code comment at the check site explicitly acknowledges this is a "cheap check" using size only.

## Finding Description
In `tx-pool/src/util.rs` (L42–45), `check_tx_fee` computes the minimum fee as:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

This uses raw serialized bytes as the sole weight denominator. However, the actual fee rate for every pool entry is computed via `get_transaction_weight` in `util/types/src/core/tx_pool.rs` (L298–303):

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` at L279. [3](#0-2) 

`TxEntry::fee_rate()` in `tx-pool/src/component/entry.rs` (L114–118) uses this weight:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [4](#0-3) 

The exploit path in `_process_tx` (`tx-pool/src/process.rs` L705–751):
1. L715: `pre_check` → `check_tx_fee` (byte-size check only, passes)
2. L720: `max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles())` — for local RPC with no declared cycles, this is 3,500,000,000 cycles
3. L724–732: `verify_rtx` → full script execution at up to `max_block_cycles()`
4. L736–749: `DeclaredWrongCycles` check only fires *after* verification completes — it does not prevent the expensive execution
5. L751: `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — entry is created with actual cycles; `fee_rate()` now reflects the true (low) effective rate [5](#0-4) 

For a transaction with 500 bytes and 70M cycles: `check_tx_fee` requires only 500 shannons (1000 × 500 / 1000). The actual weight is `max(500, 70M × 0.000_170_571_4) = max(500, 11,940) = 11,940`, giving an effective fee rate of ~42 shannons/KB — 24× below the 1,000 shannons/KB minimum.

## Impact Explanation
This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. An attacker can keep verification worker threads saturated at negligible cost (~500 shannons per multi-billion-cycle verification run for the local RPC path). Coordinated across multiple nodes, this degrades transaction processing throughput network-wide.

## Likelihood Explanation
Any unprivileged caller with access to `send_transaction` (local RPC) or any connected P2P peer can trigger this. Crafting a cycle-heavy, byte-small transaction requires only a lock script that runs a tight loop — no special privileges or victim interaction needed. The attack is repeatable and economically viable. The verify queue's `Full` rejection provides a soft bound but does not prevent the attacker from keeping the queue perpetually saturated.

## Recommendation
Replace the byte-size-only check in `check_tx_fee` with a weight-aware check:

1. If the caller declares cycles (already supported via the `declared_cycles` parameter in `_process_tx`), pass `declared_cycles` into `check_tx_fee` and compute `min_fee` against `get_transaction_weight(tx_size, declared_cycles)`.
2. If no cycles are declared (local RPC path), apply a conservative floor using `max_tx_verify_cycles * DEFAULT_BYTES_PER_CYCLES` as the minimum weight, ensuring the fee covers worst-case verification cost before any script execution occurs.

## Proof of Concept
1. Construct a CKB transaction with a lock script that executes a tight loop consuming ~70M cycles. Keep the serialized transaction size at ~500 bytes by minimizing witness and output data.
2. Call `send_transaction` via local RPC with this transaction, paying a fee of 500 shannons (passes `check_tx_fee`: `1000 * 500 / 1000 = 500`).
3. Observe the node runs `verify_rtx` → `ContextualTransactionVerifier::verify` consuming up to 70M (or up to 3.5B for the no-declared-cycles path) CKB-VM cycles.
4. The transaction is admitted with an effective fee rate of ~42 shannons/KB, far below the 1,000 shannons/KB minimum.
5. Repeat in a loop to saturate the verification worker pool. Monitor CPU usage on the node to confirm exhaustion of verification capacity at a cost of ~500 shannons per iteration.

### Citations

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
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
