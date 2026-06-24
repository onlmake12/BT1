Audit Report

## Title
Tx-Pool Admission Uses Size-Only Fee Check, Allowing Cheap Cycle-Budget Exhaustion Griefing - (File: tx-pool/src/util.rs)

## Summary
`check_tx_fee` enforces the minimum fee rate using only the transaction's serialized byte size, while the actual resource cost is measured by weight (`max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`). An attacker can craft transactions with small byte size but near-maximum cycle consumption, paying only the size-based minimum fee while consuming cycle resources worth ~60× more in weight-equivalent terms. No post-verification weight-based fee check exists, and the pool's cycle accumulation (`total_tx_cycles`) has no eviction trigger.

## Finding Description

**Root cause — size-only admission gate:**

In `tx-pool/src/util.rs` lines 42–45, `check_tx_fee` explicitly uses `tx_size` only:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The comment acknowledges the gap but treats it as acceptable. No compensating check is applied after script execution.

**No post-verification weight-based check:**

In `tx-pool/src/process.rs` lines 724–754, after `verify_rtx` returns `verified.cycles`, the code immediately constructs the `TxEntry` and calls `submit_entry` with no additional fee gate:

```rust
let verified = try_or_return_with_snapshot!(verified_ret, snapshot);
// ... declared cycles check only ...
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
```

**Weight-based fee rate used only for eviction/sorting, not admission:**

`TxEntry::fee_rate()` in `tx-pool/src/component/entry.rs` lines 114–118 uses `get_transaction_weight(self.size, self.cycles)`, and `EvictKey` in `sort_key.rs` lines 80–103 evicts by weight-based fee rate. However, this only applies *after* admission. A transaction with 200-byte size and 70M cycles gets a weight-based fee rate of ~16.7 shannons/KW — far below the 1,000 shannons/KW minimum — yet it was admitted.

**No cycle-based eviction trigger:**

`limit_size` in `tx-pool/src/pool.rs` line 298 only triggers on `total_tx_size > max_tx_pool_size`. The `total_tx_cycles` field in `pool_map.rs` lines 70–71 is tracked but never used as an eviction condition. The pool can accumulate unbounded cycles.

**Exploit flow:**
1. Deploy a script consuming ~69,999,999 cycles in a live cell dep (keeping tx size small).
2. Craft a ~200-byte transaction referencing that cell dep. Fee = 200 shannons (passes `min_fee_rate × tx_size`).
3. Submit via RPC. `check_tx_fee` passes (size-based). Script executes, consuming ~70M cycles. `TxEntry` is created with `cycles ≈ 70M`, `size ≈ 200`.
4. Actual weight-based fee rate ≈ 16.7 shannons/KW — 60× below the minimum — but the transaction is already admitted.
5. Repeat. Each submission forces the node to spend CPU on full script execution before the transaction can be evicted.

## Impact Explanation

This maps to **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

The primary impact is DoS on verification CPU resources: each submitted transaction forces the node to execute up to 70M cycles of script regardless of whether it is eventually evicted. The secondary impact is pool space pressure — while the eviction mechanism correctly evicts low-weight-fee-rate transactions first (protecting legitimate high-fee transactions from permanent displacement), the attacker can continuously resubmit evicted transactions, sustaining CPU load and pool churn. The underpayment ratio of ~60× means the attacker's cost per unit of disruption is ~60× lower than for legitimate transactions.

## Likelihood Explanation

- **Entry path**: Any unprivileged user via `send_transaction` RPC or P2P relay. No special role required.
- **Setup cost**: A high-cycle script deployed once in a cell dep; the attacker needs UTXOs to spend, but even a moderate number (hundreds) sustains meaningful CPU pressure.
- **Persistence**: Continuous resubmission of evicted transactions maintains pool pressure at low marginal cost.
- **Naturally occurring**: Complex lock scripts (ZK verifiers, multi-sig aggregation) submitted with minimum fees can trigger this without malicious intent.

## Recommendation

After `verify_rtx` returns `verified.cycles` in `_process_tx`, apply a weight-based minimum fee check before constructing the `TxEntry`:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

The existing size-only check in `check_tx_fee` can be retained as a fast pre-filter before script execution. The post-verification check closes the gap between admission cost and actual resource cost.

## Proof of Concept

1. Deploy a CKB script that loops for exactly 69,999,999 cycles. Store it in a live cell on-chain.
2. Craft a transaction: one input (spendable UTXO), one output (capacity minus fee), one cell dep referencing the high-cycle script. Serialized size ≈ 200 bytes. Fee = 200 shannons.
3. Submit via `send_transaction` RPC.
   - `check_tx_fee`: `min_fee = 1000 × 200 / 1000 = 200 shannons` ✓ passes.
   - `verify_rtx` executes the script: ~70M cycles consumed.
   - `TxEntry` created: `size=200`, `cycles=70_000_000`, `fee=200`.
   - `fee_rate() = FeeRate::calculate(200, max(200, 11940)) ≈ 16.7 shannons/KW` — 60× below minimum.
4. Repeat with different UTXOs. Observe via `tx_pool_info` that `total_tx_cycles` grows rapidly while node CPU is saturated with script verification.
5. Confirm: attacker transactions are deprioritized for block inclusion (correct) but each submission still forces full script execution on the node.