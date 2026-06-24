Audit Report

## Title
Tx-Pool Min-Fee Check Uses Serialized Size Only, Ignoring Actual Cycles Weight, Allowing High-Cycles Transactions to Bypass Effective Fee-Rate Enforcement — (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` enforces the minimum fee rate using only the transaction's serialized byte size, not its actual weight (`max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`). After `verify_rtx` resolves the true cycle count, no second fee-rate check is performed. An unprivileged submitter can craft a transaction with a small serialized size but near-maximum cycles, pass the size-only fee gate, and be admitted to the pool at an effective fee rate far below `min_fee_rate` on a weight basis — enabling cheap CPU exhaustion and pool flooding.

## Finding Description

**Root cause — `check_tx_fee` (`tx-pool/src/util.rs`, lines 42–45):**

The function computes the minimum required fee using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code comment itself acknowledges this is a proxy, not the true weight. The actual weight formula (`get_transaction_weight` in `util/types/src/core/tx_pool.rs`, lines 298–303) is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`.

**No second fee check after cycles are known (`tx-pool/src/process.rs`, lines 715–753):**

```
pre_check(&tx)          → check_tx_fee uses tx_size only
verify_rtx(...)         → cycles now known
TxEntry::new(rtx, verified.cycles, fee, tx_size)  → no re-check
submit_entry(...)       → no fee-rate re-validation
```

`_submit_entry` (lines 1016–1037) simply calls `tx_pool.add_pending/add_gap/add_proposed` with no fee-rate guard. `submit_entry` (lines 96–170) performs RBF checks and time-relative re-verification on tip change, but no weight-based fee re-check.

**Pool size limit is byte-based, not weight-based (`tx-pool/src/pool.rs`, line 298):**

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```

The 180 MB cap is on serialized bytes. A 1 KB transaction with 70 M cycles occupies only 1 KB of pool space, so ~180,000 such transactions fit simultaneously.

**Eviction uses actual weight** (`tx-pool/src/component/entry.rs`, lines 234–247), so these entries are evicted first when the pool fills — but the attacker can continuously resubmit at the same low byte-based fee cost.

**Quantified gap:** With `max_tx_verify_cycles = 70_000_000` and `DEFAULT_BYTES_PER_CYCLES ≈ 0.000_170_571_4`, a 1 KB transaction consuming 70 M cycles has weight ≈ 11,940 bytes. The size-only gate requires 1,000 shannons; the weight-based gate would require 11,940 shannons — a ~12× shortfall.

## Impact Explanation

This matches the allowed CKB bounty impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker can:
1. Flood the verify queue with small-size, high-cycles transactions, each consuming maximum CPU during `verify_rtx` (up to 70 M cycles per tx).
2. Occupy pool slots at ~1/12 the intended fee cost (byte-capped pool, not weight-capped).
3. Displace legitimate transactions via weight-based eviction, causing confirmation delays.
4. Sustain the attack indefinitely by resubmitting evicted transactions at the same low cost.

The CPU exhaustion during `verify_rtx` is the primary DoS vector: each transaction forces full script execution before being admitted or rejected.

## Likelihood Explanation

Any node reachable via the `send_transaction` RPC or P2P relay is a valid entry point. No privileged access, key material, or majority hash power is required. Crafting a small-serialized, high-cycles transaction requires only a script that loops near `max_tx_verify_cycles`. The attack is cheap to automate and sustain.

## Recommendation

After `verify_rtx` resolves the actual cycle count in `_process_tx`, perform a second fee-rate check using the true weight before calling `submit_entry`:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee_by_weight.as_u64(), fee.as_u64())), snapshot));
}
```

Alternatively, enforce a cycles-proportional fee floor at the pre-check stage using the `declared_cycles` parameter already available in `_process_tx`.

## Proof of Concept

1. Craft a transaction with serialized size ≈ 1,000 bytes and a lock/type script that consumes ≈ 70,000,000 cycles (near `max_tx_verify_cycles`).
2. Set the fee to exactly `min_fee_rate.fee(1_000)` = 1,000 shannons (at default 1,000 shannons/KW).
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 1_000 × 1_000 / 1_000 = 1_000 shannons` → passes.
5. `verify_rtx` executes the script, returning `cycles ≈ 70_000_000`.
6. Actual weight = `max(1_000, 70_000_000 × 0.000_170_571_4)` ≈ 11,940.
7. True required fee = `11,940 × 1_000 / 1_000 = 11,940 shannons` — but only 1,000 were paid.
8. `TxEntry::new(rtx, 70_000_000, 1_000_shannons, 1_000)` is created and submitted with no further fee check.
9. Repeat at scale to fill the pool with cycle-heavy, fee-light entries and exhaust verification CPU. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
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

**File:** tx-pool/src/process.rs (L1016-1037)
```rust
fn _submit_entry(
    tx_pool: &mut TxPool,
    status: TxStatus,
    entry: TxEntry,
    callbacks: &Callbacks,
) -> Result<HashSet<TxEntry>, Reject> {
    let tx_hash = entry.transaction().hash();
    debug!("submit_entry {:?} {}", status, tx_hash);
    let (succ, evicts) = match status {
        TxStatus::Fresh => tx_pool.add_pending(entry.clone())?,
        TxStatus::Gap => tx_pool.add_gap(entry.clone())?,
        TxStatus::Proposed => tx_pool.add_proposed(entry.clone())?,
    };
    if succ {
        match status {
            TxStatus::Fresh => callbacks.call_pending(&entry),
            TxStatus::Gap => callbacks.call_pending(&entry),
            TxStatus::Proposed => callbacks.call_proposed(&entry),
        }
    }
    Ok(evicts)
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

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```
