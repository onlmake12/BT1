Audit Report

## Title
Unit Mismatch in Minimum Fee Rate Admission Check: `tx_size` Used Instead of `weight` in `check_tx_fee` — (`tx-pool/src/util.rs`)

## Summary
`check_tx_fee` computes the minimum required fee using raw serialized byte size (`tx_size`) passed to `FeeRate::fee()`, which operates on weight (the composite of size and cycles). For cycle-heavy transactions where `cycles * DEFAULT_BYTES_PER_CYCLES > tx_size`, the weight exceeds `tx_size`, so the minimum fee threshold is systematically understated. No subsequent weight-based admission check exists after `verify_rtx` returns actual cycles, making this size-based check the sole gate for fee-rate admission into the tx pool.

## Finding Description
`FeeRate` is defined as *shannons per kilo-weight* and `FeeRate::fee(weight)` computes `fee_rate * weight / 1000`. [1](#0-0) [2](#0-1) 

`check_tx_fee` at line 45 passes `tx_size as u64` directly to `FeeRate::fee()` instead of the actual weight. The code comment explicitly acknowledges this is a deliberate approximation: *"Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly."* [3](#0-2) 

In `_process_tx`, the full submission path is:
1. `pre_check` → `check_tx_fee` (uses `tx_size`, the only fee-rate admission gate)
2. `verify_rtx` (returns actual `verified.cycles`)
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — entry created with actual cycles
4. `submit_entry` — no weight-based fee check performed [4](#0-3) 

After `verify_rtx` returns actual cycles, there is no rejection path based on weight-adjusted fee rate. `TxEntry::fee_rate()` correctly uses `get_transaction_weight(self.size, self.cycles)` for pool ordering and eviction, but this is not an admission check — it only determines eviction priority once the transaction is already in the pool, after relay has already occurred.

## Impact Explanation
An unprivileged attacker can craft cycle-heavy transactions (small serialized size, high cycle count) that pass `check_tx_fee` with fees significantly below the weight-based minimum. These transactions are admitted to the pool, relayed to all peers (consuming relay bandwidth network-wide), and trigger full script execution on every receiving node (consuming CPU). For a transaction with `tx_size = 200` bytes and `cycles = 5,000,000`, the attacker pays 200 shannons instead of the correct 852 shannons — a ~76% discount. This maps to **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs**: relay bandwidth and CPU are consumed across the network at a sustained discount, degrading node performance.

## Likelihood Explanation
Any unprivileged user can call `send_transaction` via RPC. Constructing a cycle-heavy transaction requires only a script that executes many VM instructions (e.g., a tight loop) within a small serialized witness. No special keys, privileges, or majority hashpower are required. The attack is repeatable: the attacker continuously submits new cycle-heavy transactions at a discount, maintaining pool pressure and relay load. The pool's eviction mechanism (which correctly uses weight) will eventually remove these transactions when the pool fills, but the relay and CPU cost is already incurred on every peer.

## Recommendation
After `verify_rtx` returns actual cycles, perform a weight-based fee check before calling `submit_entry`:

```rust
// In _process_tx, after verify_rtx:
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_weight {
    return Some((Err(Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee_weight.as_u64(), fee.as_u64())), snapshot));
}
```

Alternatively, if the size-only check is intentionally kept as a cheap pre-filter, a weight-based check must be added post-verification as the authoritative admission gate.

## Proof of Concept
With `min_fee_rate = 1000` shannons/KW (default) and a transaction:
- `tx_size = 200` bytes
- `cycles = 5,000,000`

**Actual weight:** `max(200, 5_000_000 × 0.000_170_571_4) = max(200, 852) = 852`

**Correct minimum fee (weight-based):** `1000 × 852 / 1000 = 852 shannons`

**Fee computed by `check_tx_fee` (size-based):** `1000 × 200 / 1000 = 200 shannons`

A transaction paying 200 shannons passes `check_tx_fee` at `tx-pool/src/util.rs:47`, enters the pool, and is relayed to all peers. Its true fee rate is `1000 × 200 / 852 ≈ 234` shannons/KW — well below the 1000 shannons/KW threshold. The attacker pays ~76% less than the intended minimum per transaction, enabling sustained pool spam and relay bandwidth consumption at a discount. [3](#0-2)

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

**File:** tx-pool/src/process.rs (L715-754)
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
        try_or_return_with_snapshot!(ret, submit_snapshot);
```
