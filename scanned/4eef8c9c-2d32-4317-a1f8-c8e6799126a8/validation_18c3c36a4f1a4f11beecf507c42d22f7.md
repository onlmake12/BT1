Audit Report

## Title
`check_tx_fee` Uses Serialized Size Instead of Weight for Minimum Fee Enforcement, Allowing Cycle-Heavy Transactions to Bypass `min_fee_rate` — (`tx-pool/src/util.rs`)

## Summary
`FeeRate` in CKB is defined as shannons per kilo-weight, where weight accounts for both serialized size and script execution cycles. However, `check_tx_fee`, the sole pool-admission fee-rate gate, computes the minimum required fee using only `tx_size` (bytes), not `get_transaction_weight(tx_size, cycles)`. Because `check_tx_fee` is called in `pre_check` before script verification (cycles are not yet known), and no weight-based fee check is performed after verification, a cycle-heavy transaction paying just above the size-based minimum fee is admitted with a true weight-based fee rate far below `min_fee_rate`. This enables an unprivileged attacker to repeatedly submit transactions that consume significant CPU during script verification at minimal cost, constituting a node-level and network-level DoS.

## Finding Description

`FeeRate` is defined as shannons per kilo-weight in `util/types/src/core/fee_rate.rs`:

```rust
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;  // KW = 1000
    Capacity::shannons(fee)
}
```

Weight is computed as `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)` in `util/types/src/core/tx_pool.rs`:

```rust
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

`check_tx_fee` in `tx-pool/src/util.rs` uses only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The call flow in `_process_tx` (`tx-pool/src/process.rs`) is:

1. `pre_check` → calls `check_tx_fee` with `tx_size` only (cycles unknown) — lines 715–717
2. `verify_rtx` → produces actual `verified.cycles` — lines 724–734
3. **No weight-based fee check after step 2**
4. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — line 751

After admission, all internal pool operations correctly use `get_transaction_weight`. `TxEntry::fee_rate()` at `entry.rs` lines 114–118 and `EvictKey` at lines 234–247 both use weight. The mismatch is isolated to the admission gate and `calculate_min_replace_fee` in `pool.rs` line 103.

**Concrete example** with `min_fee_rate = 1000` shannons/KW, `max_tx_verify_cycles = 70,000,000`:
- Transaction: 200 bytes serialized, 70,000,000 cycles consumed
- `weight = max(200, 70_000_000 × 0.000_170_571_4) ≈ 11,940`
- Size-based min fee (what is checked): `1000 × 200 / 1000 = 200 shannons`
- Weight-based min fee (what should be checked): `1000 × 11,940 / 1000 = 11,940 shannons`
- A transaction paying 201 shannons passes admission with a true fee rate of `≈ 16.8 shannons/KW` — **59× below** the configured floor.

The same mismatch exists in `calculate_min_replace_fee` (`pool.rs` line 103): `self.config.min_rbf_rate.fee(size as u64)` uses size instead of weight for the RBF extra-fee calculation.

## Impact Explanation

This matches the **High** impact class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

An attacker submits a stream of cycle-heavy, small-serialized-size transactions paying just above the size-based minimum fee. Each transaction:
1. Passes `check_tx_fee` (size-based check)
2. Triggers full script verification consuming up to `max_tx_verify_cycles` CPU cycles on every node that processes it (both via JSON-RPC and P2P relay)
3. Enters the pool with a true weight-based fee rate far below `min_fee_rate`

The CPU cost of verification is the primary DoS vector: the attacker pays O(tx_size) fees but imposes O(cycles) CPU cost on every verifying node. With a 59× weight multiplier, the cost-to-impact ratio is highly asymmetric. The pool's eviction mechanism (weight-based) will eventually evict these transactions when the pool fills, but the attacker can continuously resubmit, sustaining the CPU load across the network at minimal fee cost.

## Likelihood Explanation

- The entry path is fully unprivileged: any actor can call `send_transaction` via JSON-RPC or relay a transaction over P2P.
- Crafting a cycle-heavy transaction requires only a RISC-V script with a tight loop; no special privileges or victim cooperation is needed.
- The `max_tx_verify_cycles` default of 70,000,000 gives a weight multiplier of up to ~59× over size, making the bypass significant and repeatable.
- The code comment at `util.rs` lines 42–44 explicitly acknowledges the approximation, confirming this is a known limitation with real consequences rather than an unintended edge case.

## Recommendation

After `verify_rtx` returns the actual `verified.cycles` in `_process_tx`, perform a second weight-based fee check before calling `submit_entry`:

```rust
// tx-pool/src/process.rs, after verify_rtx
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

The existing size-based check in `pre_check` can remain as a cheap early filter. The authoritative weight-based check should be added post-verification when actual cycles are known.

Apply the same fix to `calculate_min_replace_fee` in `tx-pool/src/pool.rs`, passing the replacement transaction's weight (requiring cycles to be passed alongside size) instead of raw size.

## Proof of Concept

**Setup**: CKB node with default config (`min_fee_rate = 1000` shannons/KW, `max_tx_verify_cycles = 70,000,000`).

**Steps**:
1. Deploy a lock script containing a RISC-V loop that consumes ~70,000,000 cycles.
2. Construct a transaction spending a cell locked by that script, with minimal inputs/outputs (~200 bytes serialized).
3. Set the transaction fee to 201 shannons (just above `1000 × 200 / 1000 = 200` shannons size-based minimum).
4. Submit via `send_transaction` RPC.

**Expected (correct) behavior**: Rejected with `LowFeeRate` because weight-based min fee = `1000 × 11,940 / 1000 = 11,940 shannons > 201`.

**Actual behavior**: Accepted into the pool. True fee rate ≈ 16.8 shannons/KW, 59× below the configured floor.

**Sustained attack**: Repeat submission in a loop. Each iteration forces all receiving nodes to execute ~70,000,000 RISC-V cycles of script verification while the attacker pays only 201 shannons per transaction. Pool eviction will remove these entries, but continuous resubmission sustains the CPU load across the network.

**Relevant code locations**:
- [1](#0-0) 
- [2](#0-1) 
- [3](#0-2) 
- [4](#0-3) 
- [5](#0-4) 
- [6](#0-5)

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

**File:** util/types/src/core/tx_pool.rs (L276-303)
```rust
/// Equal to MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES, see ckb-chain-spec.
/// The precision is set so that the difference between MAX_BLOCK_CYCLES * DEFAULT_BYTES_PER_CYCLES
/// and MAX_BLOCK_BYTES is less than 1.
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

/// vbytes has been deprecated, renamed to weight to prevent ambiguity
#[deprecated(
    since = "0.107.0",
    note = "Please use the get_transaction_weight instead"
)]
pub fn get_transaction_virtual_bytes(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}

/// The miners select transactions to fill the limited block space which gives the highest fee.
/// Because there are two different limits, serialized size and consumed cycles,
/// the selection algorithm is a multi-dimensional knapsack problem.
/// Introducing the transaction weight converts the multi-dimensional knapsack to a typical knapsack problem,
/// which has a simple greedy algorithm.
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```

**File:** tx-pool/src/pool.rs (L101-103)
```rust
    /// min_replace_fee = sum(replaced_txs.fee) + extra_rbf_fee
    fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
        let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```
