Audit Report

## Title
Tx-Pool Admission Uses Size-Only Weight While Scoring/Eviction Uses Cycle-Adjusted Weight, Allowing Sub-Minimum-Fee-Rate Transactions to Enter the Pool — (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces `min_fee_rate` using raw serialized byte size as the weight denominator, while all post-admission scoring, prioritization, and eviction logic uses `get_transaction_weight(size, cycles)` = `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For a cycle-heavy, byte-light transaction, these two weight values diverge by up to ~24×, allowing any unprivileged submitter to craft transactions that pass the admission gate while carrying an effective fee rate far below `min_fee_rate`. The code comment at the admission site explicitly acknowledges this as a known approximation.

## Finding Description

**Admission path** (`tx-pool/src/util.rs`, lines 42–52):

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee { ... }
```

`check_tx_fee` is called inside `pre_check` (`tx-pool/src/process.rs`, line 289), which runs **before** `verify_rtx` (line 724). Because script execution has not yet occurred, actual cycles are unknown at admission time, making it structurally impossible to use `get_transaction_weight` at this stage without a declared-cycle upper bound.

**Post-admission scoring/eviction** (`tx-pool/src/component/entry.rs`, lines 114–118 and 234–247):

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

`get_transaction_weight` (`util/types/src/core/tx_pool.rs`, lines 298–303):

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` (`util/types/src/core/tx_pool.rs`, line 279).

**Exploit flow:**

1. Craft a transaction: `size = 500 bytes`, `cycles = 70,000,000` (the configured `max_tx_verify_cycles`).
2. Pay fee = `min_fee_rate × 500 / 1000` = 500 shannons (with default `min_fee_rate = 1000`).
3. Submit via `send_transaction` RPC or P2P relay.
4. `check_tx_fee` computes `min_fee = 1000 × 500 / 1000 = 500 shannons` → **passes**.
5. After verification, `TxEntry::fee_rate()` computes weight = `max(500, 70_000_000 × 0.000_170_571_4)` = **11,940**; effective fee rate = `500 × 1000 / 11,940` ≈ **41 shannons/KW** — approximately 4.1% of `min_fee_rate`.

The pool size limit (`max_tx_pool_size = 180 MB`) is tracked by byte size (`total_tx_size`), so 500-byte cycle-heavy transactions appear small. Filling the pool with 360,000 such transactions costs approximately 1.8 CKB total — a low absolute cost.

## Impact Explanation

This is a **bad design which could cause CKB network congestion with few costs** (High, 10001–15000 points). An attacker can flood the tx-pool with cycle-heavy, byte-light transactions at ~1.8 CKB total cost, causing:

- Transient `PoolIsFull` rejections for legitimate submitters (until eviction displaces the low-weight-fee-rate entries).
- Distorted fee estimation: `get_fee_rate_statistics` uses `get_transaction_weight` for confirmed blocks, while admitted transactions were checked against size-only weight, creating a systematic downward bias when cycle-heavy transactions are prevalent.
- Disproportionate cycle budget consumption relative to fee paid, degrading block quality for miners selecting by weight-based fee rate.

The eviction mechanism (`limit_size`, `pool.rs` lines 292–328) does evict these entries first (lowest weight-based fee rate), which partially mitigates persistent pool occupation, but does not prevent the transient disruption window or fee estimation distortion.

## Likelihood Explanation

The attack requires no special privilege — any RPC caller or P2P peer can submit transactions. Constructing a script that consumes close to `max_tx_verify_cycles` (70M cycles) with a small serialized size is straightforward for any script author. The divergence is maximized at high cycle counts, which are reachable by design. The attack is repeatable and cheap (~1.8 CKB to fill the 180 MB pool with 500-byte transactions).

## Recommendation

Replace the size-only weight in `check_tx_fee` with `get_transaction_weight(tx_size, declared_cycles)` so that the admission gate enforces the same fee-rate semantics as scoring and eviction. Since actual cycles are not yet known at pre-check time (before script execution), use the caller-declared cycle limit as a conservative upper bound — consistent with how `max_tx_verify_cycles` is already enforced in `_process_tx` (`process.rs`, line 720: `let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles())`). For local submissions without a declared cycle limit, use `max_tx_verify_cycles` as the upper bound.

## Proof of Concept

1. Construct a CKB transaction with a lock script that loops for ~70,000,000 cycles. Serialized size: ~500 bytes.
2. Set fee = `1000 × 500 / 1000` = 500 shannons (default `min_fee_rate = 1000 shannons/KW`).
3. Submit via `send_transaction` RPC.
4. **Expected (current behavior):** Transaction is admitted — `check_tx_fee` computes `min_fee = 500 shannons`; fee ≥ 500 → passes.
5. **Observed inconsistency:** `TxEntry::fee_rate()` computes weight = 11,940; effective fee rate ≈ 41 shannons/KW ≈ 4.1% of `min_fee_rate`.
6. Repeat with ~360,000 such transactions (total cost ~1.8 CKB) to fill the 180 MB pool, causing `PoolIsFull` rejections for honest submitters and distorting fee estimation.
7. Verify via `tx_pool_info` RPC that `total_tx_size` grows while `total_tx_cycles` reflects disproportionate cycle consumption.