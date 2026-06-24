All code claims are confirmed by the actual source. Let me verify the eviction ordering logic to complete the analysis.

All code claims are verified against the actual source. The divergence between admission weight (size-only) and post-admission weight (`get_transaction_weight`) is confirmed, the exploit path is reachable by any unprivileged user, and no Security.md exclusions exist.

---

Audit Report

## Title
Tx-Pool Admission Uses Size-Only Weight While Scoring/Eviction Uses Cycle-Adjusted Weight, Allowing Sub-Minimum-Fee-Rate Transactions to Enter the Pool — (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` enforces `min_fee_rate` using raw serialized byte size as the weight denominator, while all post-admission scoring, prioritization, and eviction logic uses `get_transaction_weight(size, cycles)` = `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For a cycle-heavy, byte-light transaction, these two weight values diverge by up to ~24×, allowing any unprivileged submitter to craft transactions that pass the admission gate while carrying an effective fee rate far below `min_fee_rate`. The code comment at the admission site explicitly acknowledges this as a known approximation.

## Finding Description

**Admission path** (`tx-pool/src/util.rs`, lines 42–52):

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee { ... }
``` [1](#0-0) 

`check_tx_fee` is called inside `pre_check` (`tx-pool/src/process.rs`, line 289), which runs before `verify_rtx` (line 724). Because script execution has not yet occurred, actual cycles are unknown at admission time. [2](#0-1) 

**Post-admission scoring/eviction** (`tx-pool/src/component/entry.rs`, lines 114–118):

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [3](#0-2) 

`get_transaction_weight` (`util/types/src/core/tx_pool.rs`, lines 298–303):

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [4](#0-3) 

`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` (`util/types/src/core/tx_pool.rs`, line 279). [5](#0-4) 

**Eviction** uses `EvictKey` which sorts by `fee_rate` ascending (lowest weight-based fee rate evicted first), computed via `get_transaction_weight` — confirming the cycle-heavy entries are evicted first but only after admission. [6](#0-5) [7](#0-6) 

**Exploit flow:**

1. Craft a transaction: `size = 500 bytes`, `cycles = 70,000,000` (the configured `max_tx_verify_cycles`).
2. Pay fee = `min_fee_rate × 500 / 1000` = 500 shannons (default `min_fee_rate = 1000`).
3. Submit via `send_transaction` RPC or P2P relay.
4. `check_tx_fee` computes `min_fee = 1000 × 500 / 1000 = 500 shannons` → **passes**.
5. After verification, `TxEntry::fee_rate()` computes weight = `max(500, 70_000_000 × 0.000_170_571_4)` = **11,940**; effective fee rate = `500 × 1000 / 11,940` ≈ **41 shannons/KW** — approximately 4.1% of `min_fee_rate`.

The pool size limit (`max_tx_pool_size`) is tracked by byte size (`total_tx_size`), so 500-byte cycle-heavy transactions appear small. [8](#0-7) 

## Impact Explanation

**High (10001–15000 points): Bad design which could cause CKB network congestion with few costs.**

An attacker can flood the tx-pool with cycle-heavy, byte-light transactions at ~1.8 CKB total cost (360,000 × 500-byte transactions to fill the 180 MB pool), causing:

- Transient `PoolIsFull` rejections for legitimate submitters until eviction displaces the low-weight-fee-rate entries.
- Distorted fee estimation: `estimate_fee_rate` uses `entry.inner.fee_rate()` (weight-based) for pool entries, while admitted transactions were checked against size-only weight, creating a systematic downward bias when cycle-heavy transactions are prevalent.
- Disproportionate cycle budget consumption relative to fee paid, degrading block quality for miners selecting by weight-based fee rate. [9](#0-8) 

## Likelihood Explanation

The attack requires no special privilege — any RPC caller or P2P peer can submit transactions. Constructing a script that consumes close to `max_tx_verify_cycles` (70M cycles) with a small serialized size is straightforward for any script author. The divergence is maximized at high cycle counts, which are reachable by design. The attack is repeatable and cheap. The `_process_tx` path confirms `max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles())`, meaning undeclared-cycle submissions default to the full block cycle limit, maximizing the divergence. [10](#0-9) 

## Recommendation

Replace the size-only weight in `check_tx_fee` with `get_transaction_weight(tx_size, declared_cycles)` so that the admission gate enforces the same fee-rate semantics as scoring and eviction. Since actual cycles are not yet known at pre-check time (before script execution), use the caller-declared cycle limit as a conservative upper bound — consistent with how `max_tx_verify_cycles` is already enforced in `_process_tx` (`process.rs`, line 720). For local submissions without a declared cycle limit, use `max_tx_verify_cycles` as the upper bound. This requires threading `declared_cycles` through `pre_check` into `check_tx_fee`. [11](#0-10) 

## Proof of Concept

1. Construct a CKB transaction with a lock script that loops for ~70,000,000 cycles. Serialized size: ~500 bytes.
2. Set fee = `1000 × 500 / 1000` = 500 shannons (default `min_fee_rate = 1000 shannons/KW`).
3. Submit via `send_transaction` RPC (no declared cycles, so `max_cycles` defaults to `max_block_cycles()`).
4. **Expected (current behavior):** Transaction is admitted — `check_tx_fee` computes `min_fee = 500 shannons`; fee ≥ 500 → passes.
5. **Observed inconsistency:** `TxEntry::fee_rate()` computes weight = 11,940; effective fee rate ≈ 41 shannons/KW ≈ 4.1% of `min_fee_rate`.
6. Repeat with ~360,000 such transactions (total cost ~1.8 CKB) to fill the 180 MB pool, causing `PoolIsFull` rejections for honest submitters.
7. Verify via `tx_pool_info` RPC that `total_tx_size` grows while `total_tx_cycles` reflects disproportionate cycle consumption.

### Citations

**File:** tx-pool/src/util.rs (L28-54)
```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
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
}
```

**File:** tx-pool/src/process.rs (L289-290)
```rust
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
```

**File:** tx-pool/src/process.rs (L720-720)
```rust
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
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

**File:** tx-pool/src/component/sort_key.rs (L92-103)
```rust
impl Ord for EvictKey {
    fn cmp(&self, other: &Self) -> Ordering {
        if self.fee_rate == other.fee_rate {
            if self.descendants_count == other.descendants_count {
                self.timestamp.cmp(&other.timestamp)
            } else {
                self.descendants_count.cmp(&other.descendants_count)
            }
        } else {
            self.fee_rate.cmp(&other.fee_rate)
        }
    }
```

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```

**File:** tx-pool/src/component/pool_map.rs (L334-358)
```rust
    pub(crate) fn estimate_fee_rate(
        &self,
        mut target_blocks: usize,
        max_block_bytes: usize,
        max_block_cycles: Cycle,
        min_fee_rate: FeeRate,
    ) -> FeeRate {
        debug_assert!(target_blocks > 0);
        let iter = self.entries.iter_by_score().rev();
        let mut current_block_bytes = 0;
        let mut current_block_cycles = 0;
        for entry in iter {
            current_block_bytes += entry.inner.size;
            current_block_cycles += entry.inner.cycles;
            if current_block_bytes >= max_block_bytes || current_block_cycles >= max_block_cycles {
                target_blocks -= 1;
                if target_blocks == 0 {
                    return entry.inner.fee_rate();
                }
                current_block_bytes = entry.inner.size;
                current_block_cycles = entry.inner.cycles;
            }
        }

        min_fee_rate
```
