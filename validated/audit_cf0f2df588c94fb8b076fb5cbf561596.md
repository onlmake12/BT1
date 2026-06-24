Audit Report

## Title
Fee Rate Admission Check Uses `tx_size` Instead of Weight, Allowing Cycle-Heavy Transactions to Bypass `min_fee_rate` — (`tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee using raw serialized byte size (`tx_size`) as the weight argument to `FeeRate::fee()`, even though `FeeRate` is defined as shannons per kilo-**weight** where weight is `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. No second fee rate check is performed after `verify_rtx` reveals actual cycle consumption, so cycle-heavy transactions enter the pool with effective fee rates far below `min_fee_rate` and are relayed to peers before any eviction can occur.

## Finding Description

`FeeRate` is defined as shannons per kilo-weight and its `fee()` method computes `rate * weight / 1000`: [1](#0-0) [2](#0-1) 

Transaction weight is `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`: [3](#0-2) 

In `check_tx_fee`, the minimum fee is computed using `tx_size` directly as the weight argument. The code comment explicitly acknowledges the mismatch but treats it as intentional: [4](#0-3) 

`check_tx_fee` is called inside `pre_check` **before** script verification, so actual cycles are not yet known: [5](#0-4) 

After `verify_rtx` returns actual cycles, `TxEntry` is created with `verified.cycles` and submitted directly — there is **no second fee rate check** using the correct weight: [6](#0-5) 

The `limit_size` eviction (which uses correct weight-based fee rate via `entry.fee_rate()`) only triggers when `total_tx_size > max_tx_pool_size` — i.e., after the transaction is already in the pool and has been relayed: [7](#0-6) 

The `fee_rate()` method on `TxEntry` correctly uses `get_transaction_weight(self.size, self.cycles)`, confirming the admission check is the only place that uses the wrong unit: [8](#0-7) 

**Concrete example with `min_fee_rate = 1000` shannons/KW:**

| Metric | Value |
|---|---|
| `tx_size` | 200 bytes |
| `cycles` | 70,000,000 |
| `weight` = `max(200, 70M × 0.000_170_571_4)` | **11,940** |
| `min_fee` via size-based check (actual code) | `1000 × 200 / 1000` = **200 shannons** |
| `min_fee` via correct weight-based check | `1000 × 11940 / 1000` = **11,940 shannons** |
| Effective fee rate if fee = 200 shannons | `200 × 1000 / 11940 ≈ 16.7 shannons/KW` |

A transaction paying 200 shannons passes `check_tx_fee` despite having an effective fee rate ~60× below `min_fee_rate`.

## Impact Explanation

This matches **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** An attacker can inject cycle-heavy transactions into the mempool at a fraction of the intended economic cost. Each such transaction: (1) consumes up to `max_tx_verify_cycles`-worth of script execution on the receiving node; (2) is relayed to peers via the P2P relay protocol, forcing every peer to also verify and temporarily store it — amplifying CPU and memory cost across the network; (3) occupies pool slots until eviction, which only occurs when the pool is full. The bypass factor scales with `cycles / tx_size`, reaching ~60× at `max_tx_verify_cycles = 70M` with a minimal transaction body, making sustained spam economically viable.

## Likelihood Explanation

Any unprivileged user can trigger this via the `send_transaction` RPC or P2P relay. No special privileges, leaked keys, or victim mistakes are required. Crafting a transaction with a small serialized body but high cycle consumption (a script that loops near the cycle limit) is standard CKB script development. The condition is deterministic and reproducible on any node with a non-zero `min_fee_rate`.

## Recommendation

After `verify_rtx` returns `verified.cycles`, perform a second fee rate check using the correct weight before calling `submit_entry`:

```rust
// In _process_tx, after verify_rtx:
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(tx_pool_config.min_fee_rate, min_fee_by_weight.as_u64(), fee.as_u64())), snapshot));
}
```

Alternatively, pass `declared_cycles` (available for remote transactions) into `check_tx_fee` and use `get_transaction_weight(tx_size, declared_cycles)` for the admission check, with a post-verification re-check using actual cycles.

## Proof of Concept

1. Configure a CKB node with `min_fee_rate = 1000` (shannons/KW).
2. Deploy a lock script that loops consuming ~70,000,000 cycles but has minimal serialized size (~200 bytes).
3. Craft a transaction spending a cell locked by that script, with fee = 200 shannons (satisfies `1000 × 200 / 1000 = 200`).
4. Submit via `send_transaction` RPC.
5. Observe the transaction is accepted into the pool. Verify via `get_transaction` that status is `pending`.
6. Check `get_pool_info` — the transaction occupies pool space with an effective fee rate of ~16.7 shannons/KW, far below `min_fee_rate = 1000`.
7. Repeat to fill the pool with below-minimum-fee-rate transactions. Observe that legitimate transactions with correct fee rates must compete with economically underpriced ones, and that peer nodes also receive and verify these transactions via relay.

### Citations

**File:** util/types/src/core/fee_rate.rs (L3-7)
```rust
/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);

const KW: u64 = 1000;
```

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
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

**File:** tx-pool/src/process.rs (L715-717)
```rust
        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);
```

**File:** tx-pool/src/process.rs (L751-753)
```rust
        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);

        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
```

**File:** tx-pool/src/pool.rs (L298-329)
```rust
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
    }
```

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```
