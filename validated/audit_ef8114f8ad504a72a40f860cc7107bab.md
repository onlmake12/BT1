Audit Report

## Title
`check_tx_fee` Enforces `min_fee_rate` Using Only `tx_size` While Actual Fee Rate Uses `weight = max(size, cycles × bytes_per_cycle)`, Allowing High-Cycle Transactions to Bypass the Minimum Fee Rate Gate - (`File: tx-pool/src/util.rs`)

## Summary

`check_tx_fee` computes the minimum required fee using only the serialized transaction byte size, while the actual fee rate stored in `TxEntry` and used for pool scoring and eviction uses `get_transaction_weight(size, cycles)` = `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. An unprivileged caller can craft a transaction whose script consumes near-maximum cycles but has a small serialized size, paying a fee that satisfies the size-only gate while the transaction's true weight-based fee rate is orders of magnitude below `min_fee_rate`. This allows systematic fee evasion and enables filling the mempool with high-cycle, low-fee-rate transactions at minimal cost.

## Finding Description

**Root cause — `check_tx_fee` (`tx-pool/src/util.rs`, L42–52):**

The gate computes `min_fee = min_fee_rate × tx_size / 1000` using only byte size. The code comment even acknowledges this is a known approximation:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [1](#0-0) 

**Actual fee rate after admission — `TxEntry::fee_rate()` (`tx-pool/src/component/entry.rs`, L114–118):**

Once admitted, the entry's fee rate is computed via `get_transaction_weight`, which uses `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [2](#0-1) 

**`get_transaction_weight` definition (`util/types/src/core/tx_pool.rs`, L298–303):** [3](#0-2) 

**Exploit flow:**

1. `pre_check` calls `check_tx_fee` before script execution — cycles are unknown at this point, so only `tx_size` is used. [4](#0-3) 

2. After script execution, `TxEntry::new` is created with the actual verified cycles. [5](#0-4) 

3. The entry is admitted with `cycles ≈ 69,000,000`, giving `weight ≈ 11,769` bytes and actual `fee_rate ≈ 16 shannons/KW` — 62× below `min_fee_rate = 1000`.

**Concrete numeric example** (default config: `min_fee_rate = 1000`, `max_tx_verify_cycles = 70,000,000`): [6](#0-5) 

| Parameter | Value |
|---|---|
| `tx_size` | 200 bytes |
| `cycles` | 69,000,000 |
| `weight` | `max(200, 69M × 0.000_170_571_4)` ≈ **11,769** |
| `min_fee` (gate) | `1000 × 200 / 1000` = **200 shannons** |
| Actual fee rate | `200 × 1000 / 11,769` ≈ **16 shannons/KW** |
| Intended minimum | **1000 shannons/KW** |

**Eviction does not prevent the attack:** `limit_size` evicts by actual fee rate (weight-based), so these transactions are evicted first — but the attacker can continuously resubmit them, causing persistent disruption and displacing legitimate transactions. [7](#0-6) 

## Impact Explanation

This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points).**

- The `min_fee_rate` policy is bypassed. Transactions enter the pool paying ~62× less than the node operator intends to require.
- The pool size limit is 180 MB measured in bytes (`total_tx_size`). At 200 bytes per transaction, an attacker needs ~900,000 transactions to fill the pool, costing only 900,000 × 200 = 180,000,000 shannons = 1.8 CKB — trivially cheap.
- These transactions are relayed to peer nodes, which also accept them (same gate), causing network-wide mempool congestion.
- Legitimate higher-fee-rate transactions are displaced from the pool when `limit_size` eviction runs. [8](#0-7) 

## Likelihood Explanation

- **Entry path**: Any unprivileged user via `send_transaction` JSON-RPC. No special role or key required.
- **Ease of exploit**: A RISC-V tight loop in a CKB lock/type script can consume an arbitrary number of cycles up to `max_tx_verify_cycles`. The attacker directly controls cycles through script content.
- **Deterministic**: The discrepancy is structural and always present when `cycles × DEFAULT_BYTES_PER_CYCLES > tx_size`, i.e., when cycles exceed `tx_size / 0.000_170_571_4 ≈ 5,862 × tx_size`. For a 200-byte transaction, this threshold is only ~1,172,400 cycles — far below the 70M maximum.
- **No special conditions**: Default configuration on any standard node is sufficient. [9](#0-8) 

## Recommendation

Perform a second fee rate check after script execution (when actual cycles are known) and reject the transaction if its actual weight-based fee rate is below `min_fee_rate`. The correct enforcement point is at line 751 of `tx-pool/src/process.rs`, after `verified.cycles` is available and before `submit_entry` is called:

```rust
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
// Add weight-based fee rate check here:
let actual_fee_rate = entry.fee_rate();
if actual_fee_rate < tx_pool_config.min_fee_rate {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

Alternatively, use `max_tx_verify_cycles` as a conservative upper bound in `check_tx_fee` to compute a weight-based minimum fee at pre-check time. [5](#0-4) 

## Proof of Concept

1. Deploy a CKB lock script containing a RISC-V tight loop consuming ~69,000,000 cycles (just under `max_tx_verify_cycles = 70,000,000`).
2. Create a cell locked by this script. The serialized transaction spending it will be small (~200 bytes).
3. Set fee = `min_fee_rate × tx_size / 1000 = 1000 × 200 / 1000 = 200` shannons — exactly the gate threshold.
4. Submit via `send_transaction` RPC.
5. **Observation**: The transaction passes `check_tx_fee` (200 ≥ 200 shannons). After script execution, `verified.cycles ≈ 69,000,000`, giving `weight ≈ 11,769` and actual `fee_rate ≈ 16 shannons/KW` — 62× below `min_fee_rate`.
6. Create many UTXOs via a single splitting transaction, then repeat step 3–5 for each UTXO.
7. **Result**: The pool fills with sub-minimum-fee-rate transactions at a cost of ~1.8 CKB for 180 MB of pool space, causing network-wide mempool congestion as these transactions are relayed to peers. [1](#0-0)

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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
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

**File:** tx-pool/src/process.rs (L288-290)
```rust
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
```

**File:** tx-pool/src/process.rs (L751-753)
```rust
        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);

        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
```

**File:** util/app-config/src/legacy/tx_pool.rs (L9-14)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
// default max tx verify cycles
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
```

**File:** tx-pool/src/pool.rs (L290-328)
```rust
    // Remove transactions from the pool until total size <= size_limit.
    // Return a `Reject` for current inserting entry if it's removed
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
                }
            }
        }
        self.pool_map.entries.shrink_to_fit();
        ret
```
