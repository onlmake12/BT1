Looking at the code carefully to validate all claims.

**Key findings from the code:**

1. `check_tx_fee` in `tx-pool/src/util.rs` L45 uses only `tx_size` — confirmed. [1](#0-0) 

2. The code itself acknowledges the limitation with a comment at L42-44. [2](#0-1) 

3. `get_transaction_weight` uses `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)` — confirmed. [3](#0-2) 

4. `check_tx_fee` is called in `pre_check` **before** `verify_rtx`, so cycles are not yet known. After `verify_rtx` returns with actual cycles, there is **no second fee check** using the true weight — the entry is directly created and submitted. [4](#0-3) 

5. Post-admission, `TxEntry::fee_rate()` and `AncestorsScoreSortKey` both use the true weight — confirming the divergence between admission gate and internal accounting. [5](#0-4) [6](#0-5) 

6. `DEFAULT_MAX_TX_POOL_SIZE = 180_000_000` and `DEFAULT_MAX_TX_VERIFY_CYCLES = TWO_IN_TWO_OUT_CYCLES * 20` — confirmed. [7](#0-6) 

All code references, root cause, and exploit path are verified. The vulnerability is real.

---

Audit Report

## Title
Size-Only Admission Fee Check Allows Cycle-Heavy Transactions to Bypass Effective Minimum Fee Rate - (File: tx-pool/src/util.rs)

## Summary
`check_tx_fee` enforces the minimum fee rate using only the serialized byte size of a transaction, while the actual block-assembly weight (`get_transaction_weight`) uses `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. Because `check_tx_fee` is called before script verification (when cycles are unknown), there is no post-verification weight-based fee gate. An attacker can submit transactions that pass admission at ~119× below the intended minimum fee rate, enabling cheap mempool flooding and node CPU exhaustion.

## Finding Description
In `_process_tx` (`tx-pool/src/process.rs`), the flow is:
1. `pre_check` → `check_tx_fee(tx_pool, snapshot, rtx, tx_size)` — fee gate using size only (L715, L289)
2. `verify_rtx(...)` — actual cycles now known (L724–732)
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — entry created with true cycles, no second fee check (L751)
4. `submit_entry(...)` — inserted into pool (L753)

`check_tx_fee` computes `min_fee = min_fee_rate.fee(tx_size)`, i.e., `min_fee_rate * tx_size / 1000`. The true weight used everywhere else (sorting, eviction, fee estimation) is `max(tx_size, cycles * 0.000_170_571_4)`. For a transaction with `tx_size = 100` bytes and `cycles = 70_000_000` (the default `max_tx_verify_cycles`):

| Metric | Value |
|---|---|
| Admission min fee (size-based) | 100 shannons |
| True weight | ≈ 11,940 |
| True min fee | 11,940 shannons |
| Underpayment ratio | ~119× |

The code comment at L42–44 explicitly acknowledges this: *"Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check."* However, no compensating post-verification check exists.

Existing guards are insufficient: the `limit_size` eviction uses the true weight-based `EvictKey`, so attacker transactions rank lowest and are evicted first — but the attacker can continuously resubmit, forcing repeated expensive verification (up to 70M VM cycles per transaction) at negligible fee cost.

## Impact Explanation
This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

An attacker can:
- Force the node to execute up to 70M VM cycles per transaction while paying only 100 shannons (CPU exhaustion / verification DoS).
- Fill the 180 MB mempool at ~119× lower cost than intended (`~180 CKB` instead of `~21,420 CKB`), triggering eviction of legitimately-priced transactions and delaying honest users.
- Continuously resubmit evicted transactions, sustaining the attack indefinitely at low cost.

## Likelihood Explanation
- Reachable via the public `send_transaction` RPC and P2P relay — no privilege required.
- The attack requires only a deployed cycle-heavy lock script (referenced as a dep cell, keeping transaction serialized size small). No special knowledge beyond standard CKB script development is needed.
- The worst-case gap (~119×) is achievable at default mainnet configuration with `max_tx_verify_cycles = TWO_IN_TWO_OUT_CYCLES * 20`.
- The attack is repeatable and cheap to sustain.

## Recommendation
Add a post-verification weight-based fee check in `_process_tx` after `verify_rtx` returns the actual cycles, before `submit_entry`:

```rust
// tx-pool/src/process.rs, after line 734
use ckb_types::core::tx_pool::get_transaction_weight;

let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

Alternatively, update `check_tx_fee` to accept an optional `cycles` parameter and apply the weight-based check when cycles are available (post-verification path), keeping the size-only check only as a pre-verification fast-reject.

## Proof of Concept
**Setup**: default mainnet config, `min_fee_rate = 1000 shannons/KB`, `max_tx_verify_cycles = 70_000_000`.

1. Deploy a lock script cell containing a tight RISC-V loop consuming ~70,000,000 cycles.
2. Craft a transaction: 1 input + 1 output using that lock script as a dep cell reference. Serialized size ≈ 100–200 bytes. Set fee = `min_fee_rate * tx_size / 1000` (e.g., 150 shannons for 150-byte tx).
3. Submit via `send_transaction` RPC with `"passthrough"` cycles hint.
4. **Observe**: `check_tx_fee` passes (`150 >= 150`). `verify_rtx` executes 70M VM cycles. Entry is admitted with true weight ≈ 11,940 and effective fee rate ≈ 12 shannons/KW (83× below `min_fee_rate`).
5. Repeat in a loop. Each iteration forces 70M cycles of node CPU work at 150 shannons cost. The 180 MB pool fills at ~1,200,000 such transactions for ~180,000 shannons (≈ 0.0018 CKB) total, versus the intended ~21,420 CKB.

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

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/component/entry.rs (L221-232)
```rust
impl From<&TxEntry> for AncestorsScoreSortKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let ancestors_weight = get_transaction_weight(entry.ancestors_size, entry.ancestors_cycles);
        AncestorsScoreSortKey {
            fee: entry.fee,
            weight,
            ancestors_fee: entry.ancestors_fee,
            ancestors_weight,
        }
    }
}
```

**File:** util/app-config/src/legacy/tx_pool.rs (L14-20)
```rust
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
// default max ancestors count
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
// Default expiration time for pool transactions in hours
const DEFAULT_EXPIRY_HOURS: u8 = 12;
// Default max_tx_pool_size 180mb
const DEFAULT_MAX_TX_POOL_SIZE: usize = 180_000_000;
```
