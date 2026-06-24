Audit Report

## Title
`check_tx_fee` Uses Serialized Size Instead of Canonical Weight for Minimum Fee Admission, Allowing Cycle-Heavy Transactions to Bypass Fee Rate Enforcement - (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size as the weight, while the canonical weight used for pool scoring, eviction, and fee statistics is `get_transaction_weight(tx_size, cycles) = max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For cycle-dominated transactions, the admission check is up to ~119× too lenient, allowing any unprivileged RPC caller to force expensive script execution on nodes at a fraction of the correct cost. The discrepancy is explicitly acknowledged in a code comment but left unresolved.

## Finding Description

`check_tx_fee` in `tx-pool/src/util.rs` (L42–52) computes the minimum required fee using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [1](#0-0) 

This function is called in `pre_check` (called from `_process_tx`) **before** `verify_rtx` executes the scripts and returns actual cycles: [2](#0-1) 

After `verify_rtx` returns `verified.cycles`, there is **no second fee check** using the canonical weight. The entry is created and submitted directly: [3](#0-2) 

The canonical weight function used everywhere else (pool sorting, eviction, fee rate statistics) is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
``` [4](#0-3) 

`TxEntry::fee_rate()` uses this canonical weight: [5](#0-4) 

The two formulas agree only when `tx_size >= cycles * DEFAULT_BYTES_PER_CYCLES`. For cycle-dominated transactions, the admission check is far too lenient. The critical exploit window is that `verify_rtx` (expensive script execution) is called **after** the size-only fee check passes, meaning the attacker forces the node to execute up to `max_tx_verify_cycles` cycles of script work before any cycle-aware fee enforcement occurs.

## Impact Explanation

**Concrete example** with `min_fee_rate = 1000` shannons/KW and `max_tx_verify_cycles = 70,000,000`:

| Parameter | Value |
|---|---|
| `tx_size` | 100 bytes |
| `cycles` | 70,000,000 |
| Actual weight | `max(100, 70_000_000 × 0.000_170_571_4)` = **11,940** |
| Min fee (admission, size-only) | `1000 × 100 / 1000` = **100 shannons** |
| Min fee (canonical weight) | `1000 × 11,940 / 1000` = **11,940 shannons** |
| Discrepancy | **~119×** |

An attacker pays 100 shannons to force the node to execute 70M cycles of script verification. Repeating this at scale causes sustained CPU exhaustion on individual nodes and propagates across the network as each relaying node must also verify. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**. Secondary impacts include fee market distortion (accepted entries have effective fee rate far below `min_fee_rate`) and miner revenue loss.

## Likelihood Explanation

The attack requires only an unprivileged `send_transaction` RPC call. The attacker needs:
1. A valid CKB transaction (valid inputs/outputs with sufficient capacity to cover the small fee).
2. A lock or type script that runs a tight loop consuming close to `max_tx_verify_cycles` cycles with minimal witness data.
3. No special privilege, key, or majority hashpower.

The entry path is confirmed at `pre_check` → `check_tx_fee` → `verify_rtx`, where the size-only check passes before the expensive verification runs. The default `max_tx_verify_cycles = 70,000,000` produces a ~119× discrepancy, making this practically exploitable on mainnet. The attacker's cost per forced verification is ~100 shannons (negligible), while the node bears the full CPU cost of 70M cycles of script execution per transaction.

## Recommendation

After `verify_rtx` returns `verified.cycles`, perform a second fee check using the canonical weight before calling `submit_entry`:

```rust
// In _process_tx, after verify_rtx returns verified.cycles:
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(
        tx_pool.config.min_fee_rate,
        min_fee_by_weight.as_u64(),
        fee.as_u64(),
    )), snapshot));
}
```

This closes the gap completely without requiring any declared-cycles hint, since actual cycles are available at that point. The existing size-only pre-check in `check_tx_fee` can remain as a fast early rejection for clearly under-fee transactions, but the canonical weight check must be enforced post-verification.

## Proof of Concept

1. Deploy a CKB lock script that runs a tight loop consuming ~70,000,000 cycles (e.g., a simple counter loop in CKB-VM).
2. Craft a transaction spending a cell locked by this script, with `tx_size ≈ 100–200 bytes` and fee = 100–200 shannons (passing the size-only check: `1000 × 200 / 1000 = 200`).
3. Submit via `send_transaction` RPC.
4. Observe the node executes ~70M cycles of script verification before accepting or evicting the transaction.
5. Confirm the accepted entry's effective fee rate via `get_pool_tx_verbose` — it will report `fee_rate ≈ 8 shannons/KW`, far below the configured `min_fee_rate = 1000 shannons/KW`.
6. Repeat in a loop (each iteration costs ~100–200 shannons) to sustain CPU load on the target node and any nodes it relays to.

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

**File:** tx-pool/src/process.rs (L715-734)
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
```

**File:** tx-pool/src/process.rs (L751-754)
```rust
        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);

        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
        try_or_return_with_snapshot!(ret, submit_snapshot);
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

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```
