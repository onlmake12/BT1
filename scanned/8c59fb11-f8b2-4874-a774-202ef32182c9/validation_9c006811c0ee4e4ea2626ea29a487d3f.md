### Title
Fee Adequacy Check Uses Serialized Size Only, Allowing High-Cycle Transactions to Bypass the Effective `min_fee_rate` - (File: `tx-pool/src/util.rs`)

---

### Summary

The `check_tx_fee` function in the tx-pool admission path validates the minimum fee using only the transaction's serialized byte size (`tx_size`), while the actual weight used for pool ordering, block selection, and fee-rate statistics incorporates both byte size and cycle consumption via `get_transaction_weight`. A transaction sender can craft a transaction with a small serialized size but very high cycle consumption, pass the fee gate cheaply, and enter the pool with an effective fee rate far below `min_fee_rate`. This is a direct analog to the Wormhole "gas quote mismatch" class: the resource cost that is quoted at admission time differs from the resource cost that is actually consumed.

---

### Finding Description

**Root cause — `tx-pool/src/util.rs`, `check_tx_fee`, line 45:**

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The minimum fee is computed as `min_fee_rate * tx_size / 1000`. The code's own comment acknowledges this is theoretically incorrect. [1](#0-0) 

**Actual weight used everywhere else — `util/types/src/core/tx_pool.rs`, `get_transaction_weight`:**

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

The constant `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` means that at the consensus maximum of 70,000,000 cycles, the cycle-equivalent weight is approximately **11,940 bytes**, regardless of the transaction's actual serialized size. [3](#0-2) 

**Pool ordering uses full weight — `tx-pool/src/component/entry.rs`, `fee_rate`:**

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [4](#0-3) 

**The admission flow — `tx-pool/src/process.rs`, `pre_check`:**

`check_tx_fee` is called with `tx_size` before cycles are known (before script verification). After verification, the entry is stored with the actual `verified.cycles`, creating the permanent mismatch. [5](#0-4) [6](#0-5) 

**Concrete numerical example:**

| Parameter | Value |
|---|---|
| `tx_size` | 300 bytes |
| `cycles` | 70,000,000 (max) |
| `cycle_weight` | `70,000,000 × 0.000_170_571_4 ≈ 11,940` bytes |
| `actual_weight` | `max(300, 11940) = 11,940` |
| Fee required by `check_tx_fee` | `1000 × 300 / 1000 = 300 shannons` |
| Effective fee rate in pool | `300 × 1000 / 11940 ≈ 25 shannons/KW` |
| Configured `min_fee_rate` | `1000 shannons/KW` |

The transaction passes admission at 1000 shannons/KW but occupies pool and block resources at an effective rate of ~25 shannons/KW — a **40× undercharge** for cycle-heavy scripts.

---

### Impact Explanation

1. **Mempool pollution by underpriced transactions**: Any unprivileged tx-pool submitter (via `send_transaction` RPC or P2P relay) can flood the pool with transactions that pay byte-size fees but consume the block's cycle budget. The pool's `max_tx_pool_size` limit is enforced by total byte size, not by weight, so cycle-heavy transactions consume disproportionate block resources relative to their fee.

2. **Miner revenue loss**: Miners select transactions by `AncestorsScoreSortKey` which uses the full weight. A cycle-heavy transaction that paid only byte-size fees will appear as a low-fee-rate entry and displace legitimately priced transactions from blocks, reducing miner revenue per block.

3. **Fee estimation distortion**: Both `FeeRateCollector::statistics` (historical) and `PoolMap::estimate_fee_rate` (fallback) use the full weight. Underpriced high-cycle transactions in the pool or committed blocks will pull fee estimates downward, causing honest users to underpay and experience delayed confirmation. [7](#0-6) [8](#0-7) 

---

### Likelihood Explanation

- **Entry path**: Reachable by any unprivileged actor via the `send_transaction` JSON-RPC or the P2P relay protocol (`TransactionsProcess::execute`). No special privilege is required.
- **Ease of exploitation**: A script author simply deploys a lock or type script that loops for many cycles. The transaction's serialized size can be kept small (a few hundred bytes) while consuming the full `max_tx_verify_cycles = 70,000,000` budget.
- **Detection**: The node itself logs nothing about this discrepancy; the comment in the code explicitly acknowledges the approximation is intentional for performance. [9](#0-8) 

---

### Recommendation

Replace the size-only fee check in `check_tx_fee` with a weight-based check. Since cycles are not yet known at `pre_check` time (before script verification), use the declared cycles from the relay message as an upper bound for the weight check, or apply the weight-based check after verification and before pool insertion:

```rust
// After verification, before TxEntry::new:
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```

Alternatively, use the declared cycles (already available in the relay path) as a conservative weight estimate during `pre_check`, consistent with how `max_cycles` is set at line 720 of `process.rs`. [10](#0-9) 

---

### Proof of Concept

1. Deploy a lock script that executes a tight loop consuming ~70,000,000 cycles. The script binary itself can be stored in a cell dep; the transaction's serialized size remains ~300 bytes.
2. Submit via `send_transaction` RPC with `fee = 300 shannons` (satisfying `min_fee_rate × tx_size = 1000 × 300 / 1000`).
3. Observe the transaction is accepted into the pool.
4. Query `get_pool_tx_detail_info` — the `score_sortkey.weight` will reflect the cycle-dominated weight (~11,940), giving an effective fee rate of ~25 shannons/KW, far below the configured `min_fee_rate = 1000 shannons/KW`.
5. Repeat with many such transactions to exhaust the pool's cycle budget while paying only byte-size fees, displacing legitimately priced transactions.

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

**File:** util/types/src/core/tx_pool.rs (L276-280)
```rust
/// Equal to MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES, see ckb-chain-spec.
/// The precision is set so that the difference between MAX_BLOCK_CYCLES * DEFAULT_BYTES_PER_CYCLES
/// and MAX_BLOCK_BYTES is less than 1.
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

**File:** tx-pool/src/process.rs (L286-294)
```rust
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
```

**File:** tx-pool/src/process.rs (L719-721)
```rust
        let verify_cache = self.fetch_tx_verify_cache(&tx).await;
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
        let tip_header = snapshot.tip_header();
```

**File:** tx-pool/src/process.rs (L736-751)
```rust
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

**File:** rpc/src/util/fee_rate.rs (L97-106)
```rust
                if let Some(cycles) = cycles {
                    for (fee, cycles, size) in itertools::izip!(
                        txs_fees,
                        cycles,
                        txs_sizes.iter().skip(1) // skip cellbase (first element in the Vec)
                    ) {
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
                        }
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
