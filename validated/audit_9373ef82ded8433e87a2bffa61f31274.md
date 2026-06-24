Audit Report

## Title
Tx-Pool Min-Fee Check Uses Serialized Size Only, Ignoring Actual Cycles Weight, Allowing High-Cycles Transactions to Bypass Effective Fee-Rate Enforcement — (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` enforces the minimum fee rate using only the transaction's serialized byte size, not its actual weight (`max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`). After `verify_rtx` resolves the true cycle count, no second fee-rate check is performed. An unprivileged submitter can craft a transaction with a small serialized size but near-maximum cycles, pass the size-only fee gate, and be admitted to the pool at an effective fee rate up to ~12× below `min_fee_rate`.

## Finding Description

**Root cause — `check_tx_fee` (`tx-pool/src/util.rs` L42–45):**

The function explicitly uses `tx_size` as a proxy for weight, with a comment acknowledging this is only a "cheap check":

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The actual weight formula (`util/types/src/core/tx_pool.rs` L298–303) is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`.

**No second fee check after cycles are known (`tx-pool/src/process.rs` L715–753):**

```
pre_check() → check_tx_fee(tx_size only) → verify_rtx() → cycles now known
→ TxEntry::new(rtx, verified.cycles, fee, tx_size) → submit_entry()
```

After `verify_rtx` returns `verified.cycles`, the code immediately constructs `TxEntry` and calls `submit_entry` with no re-validation of fee against the true weight. Neither `submit_entry` nor `_submit_entry` performs a fee-rate check against actual weight.

**Pool capacity is byte-capped, not weight-capped (`tx-pool/src/pool.rs` L298):**

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```

`max_tx_pool_size = 180_000_000` (180 MB). A 1,000-byte transaction occupies 1,000 bytes regardless of its 70M cycles, so up to 180,000 such transactions can fill the pool simultaneously.

**Eviction uses actual weight (`tx-pool/src/component/entry.rs` L115–118):**

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

The eviction key correctly uses actual weight, meaning these low-fee-rate entries are evicted first — but only after they have already been admitted and consumed CPU during `verify_rtx`.

**Quantified gap:** With `max_tx_verify_cycles = 70_000_000` (confirmed in `resource/ckb.toml` L215) and `DEFAULT_BYTES_PER_CYCLES ≈ 0.000_170_571_4`, a 1,000-byte transaction consuming 70M cycles has effective weight ≈ 11,940. The fee check requires only `1,000 × 1,000 / 1,000 = 1,000 shannons`, while the true weight-based requirement is `11,940 shannons` — a ~12× shortfall.

## Impact Explanation

This matches the allowed CKB bounty impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points).**

An attacker can:
1. Flood the tx-pool with 180,000 high-cycles, small-size transactions (filling the 180 MB byte cap) at ~1/12 the intended fee cost.
2. Force each node to execute 70M cycles of script verification per transaction during `verify_rtx`, consuming significant CPU.
3. Continuously resubmit after eviction (eviction is triggered by `limit_size` only when the pool is full), sustaining CPU pressure at a fraction of the intended cost.
4. Displace legitimate transactions via eviction (since these entries have the lowest actual fee rate), causing confirmation delays for honest users.

## Likelihood Explanation

Any node reachable via the `send_transaction` RPC or the P2P relay path is a valid entry point. Crafting a small-serialized, high-cycles transaction requires only a script that loops near `max_tx_verify_cycles`; no privileged access, key material, or majority hash power is needed. The attack is cheap to automate and sustain.

## Recommendation

After `verify_rtx` resolves the actual cycle count in `_process_tx`, perform a second fee-rate check using the true weight before calling `submit_entry`:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(actual_weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(min_fee_rate, min_fee_by_weight.as_u64(), fee.as_u64())), snapshot));
}
```

Alternatively, enforce a cycles-proportional fee floor at the pre-check stage using the `declared_cycles` parameter already available in `_process_tx` (L708), which would allow early rejection before the expensive `verify_rtx` call.

## Proof of Concept

1. Craft a transaction with serialized size ≈ 1,000 bytes and a lock/type script that loops to consume ≈ 70,000,000 cycles.
2. Set the fee to exactly `min_fee_rate.fee(1_000)` = 1,000 shannons (at default 1,000 shannons/KW).
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 1,000 × 1,000 / 1,000 = 1,000 shannons` → passes.
5. `verify_rtx` executes the script, returning `cycles ≈ 70,000,000`.
6. No second fee check is performed. `TxEntry::new(rtx, 70_000_000, 1_000_shannons, 1_000)` is created and submitted.
7. Actual weight = `max(1,000, 70,000,000 × 0.000_170_571_4)` ≈ 11,940. True required fee = 11,940 shannons — but only 1,000 were paid.
8. Repeat at scale. With 180 MB pool and 1,000-byte transactions, up to 180,000 such entries can coexist, each having consumed 70M cycles of CPU during admission.
9. Attacker resubmits continuously after eviction to sustain the attack. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
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

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** resource/ckb.toml (L211-215)
```text
max_tx_pool_size = 180_000_000 # 180mb
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
max_tx_verify_cycles = 70_000_000
```
