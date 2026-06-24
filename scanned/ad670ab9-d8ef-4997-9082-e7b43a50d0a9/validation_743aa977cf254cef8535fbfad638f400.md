Audit Report

## Title
Fee Rate Admission Check Uses Byte Size While Pool Ordering Uses Cycles-Weighted Size, Allowing Effective Fee Rate Bypass — (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` gates pool admission using only the transaction's serialized byte size to compute the minimum required fee. The actual weight used for pool ordering, eviction, and block assembly is `get_transaction_weight`, which takes `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. An unprivileged peer can craft a transaction with a small byte footprint but high declared cycles that passes the admission gate while carrying an effective fee rate far below `min_fee_rate`, forcing the node to execute expensive script verification at below-floor cost.

## Finding Description

**Admission gate (byte-size only)** in `tx-pool/src/util.rs` L42–45:
```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```
The code comment itself acknowledges the theoretical mismatch but treats it as an acceptable trade-off for a "cheap check."

**Actual weight used for ordering/eviction** in `util/types/src/core/tx_pool.rs` L298–303:
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```
`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`, so 70 000 000 cycles → weight ≈ 11 940 bytes, roughly 12× a 1 000-byte transaction.

`TxEntry::fee_rate()` and `EvictKey` both use `get_transaction_weight` with the verified cycles stored in the entry (`tx-pool/src/component/entry.rs` L114–118, L234–247).

**Two-step flow in `_process_tx`** (`tx-pool/src/process.rs` L715–751):
1. `pre_check` (read lock) calls `check_tx_fee` with `tx_size` only — no cycles known yet.
2. `verify_rtx` runs with `declared_cycles` as the cycle cap.
3. `DeclaredWrongCycles` check passes when `declared == verified.cycles`.
4. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` is created — now the entry carries the full cycle count and its `fee_rate()` / `EvictKey` reflect the true (lower) effective fee rate.

**`declared_cycles` validation** (`sync/src/relayer/transactions_process.rs` L63–74) only rejects if `declared_cycles > max_block_cycles`. Any value ≤ `max_block_cycles` (≈ 70 000 000 cycles by default) is accepted.

**Exploit path:**
1. Craft a transaction: ~1 000 bytes serialized, script loops for ~70 000 000 cycles.
2. Pay fee = `ceil(min_fee_rate * 1_000 / 1_000)` = 1 shannon (at default 1 000 shannons/KW).
3. Relay via `RelayTransactions` with `declared_cycles = 70_000_000`.
4. `check_tx_fee`: `min_fee = 1_000 * 1_000 / 1_000 = 1` shannon; fee ≥ 1 shannon → **passes**.
5. `verify_rtx` executes up to 70 000 000 cycles of script verification.
6. `DeclaredWrongCycles` check: declared == actual → **passes**.
7. Transaction enters pool with effective fee rate ≈ 0.08 shannons/KW (≈12× below `min_fee_rate`).
8. Repeat continuously to sustain CPU pressure; each iteration costs only 1 shannon.

## Impact Explanation

This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

- The fee rate floor invariant is violated: transactions with effective fee rates well below `min_fee_rate` are admitted.
- Each admitted transaction forces the node to execute up to `max_tx_verify_cycles` cycles of script verification while the attacker pays only the byte-size-based minimum fee.
- Submitting many such transactions in parallel saturates the async verification worker pool (`max_tx_verify_workers`).
- Admitted transactions are immediately candidates for eviction (lowest actual fee rate), but the verification CPU cost has already been paid. The attacker can continuously re-submit to sustain the load at negligible cost.

## Likelihood Explanation

Any unprivileged peer reachable via the standard P2P relay protocol can trigger this. Crafting a high-cycle, small-byte transaction requires only writing a RISC-V script that loops — no privileged access, key material, or majority hashpower is needed. The relay path (`RelayTransactions`) is always open. The attack is repeatable and cheap: each iteration costs only the byte-size-based minimum fee (1 shannon at default settings).

## Recommendation

Replace the byte-size-only fee check in `check_tx_fee` with the same weight function used everywhere else in the pool. Thread `declared_cycles` into `check_tx_fee` (it is already available in `_process_tx` as `declared_cycles`):

```rust
// Instead of:
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);

// Use:
let weight = get_transaction_weight(tx_size, declared_cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

For local (non-remote) transactions where `declared_cycles` is not yet known, use `max_block_cycles` as a conservative upper bound, or defer the fee check until after verification when actual cycles are known.

## Proof of Concept

```
1. Write a CKB lock script that loops for ~70_000_000 cycles (RISC-V loop).
2. Build a transaction:
   - 1 input cell (small lock args → serialized size ≈ 1_000 bytes)
   - 1 output cell
   - fee = 1 shannon (satisfies min_fee_rate * 1_000 bytes / 1_000 = 1 shannon)
3. Relay via RelayTransactions with declared_cycles = 70_000_000.
4. Node: check_tx_fee sees tx_size=1000, min_fee=1, fee=1 → OK (passes).
5. Node: verify_rtx executes ~70M cycles of script verification.
6. Node: DeclaredWrongCycles check passes (declared == actual).
7. Transaction enters pool with actual fee_rate ≈ 0.08 shannons/KW (12× below min_fee_rate).
8. Repeat from step 3 to sustain CPU pressure at 1 shannon per iteration.
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/component/entry.rs (L234-247)
```rust
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);

        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),
            timestamp: entry.timestamp,
            descendants_count: entry.descendants_count,
        }
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

**File:** sync/src/relayer/transactions_process.rs (L63-74)
```rust
        let max_block_cycles = self.relayer.shared().consensus().max_block_cycles();
        if txs
            .iter()
            .any(|(_, declared_cycles)| declared_cycles > &max_block_cycles)
        {
            self.nc.ban_peer(
                self.peer,
                DEFAULT_BAN_TIME,
                String::from("relay declared cycles greater than max_block_cycles"),
            );
            return Status::ok();
        }
```
