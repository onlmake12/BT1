Audit Report

## Title
Fee Rate Admission Gate Uses Raw Byte Size Instead of Transaction Weight, Enabling Cheap Compute-Heavy Tx-Pool Spam - (`tx-pool/src/util.rs`)

## Summary

`check_tx_fee` enforces `min_fee_rate` using raw serialized byte size as the weight denominator, while `FeeRate` is defined as shannons per kilo-weight and all other pool subsystems (sorting, eviction, fee estimation, statistics) use `get_transaction_weight(tx_size, cycles)`. Because cycles are not yet known at the pre-verification admission stage and no post-verification fee check is performed, an attacker can submit transactions with minimal byte size but maximum cycles, paying up to ~60× less than the intended minimum fee to enter the pool while consuming full script verification CPU resources.

## Finding Description

**Root cause — `check_tx_fee` uses `tx_size` as weight:**

In `tx-pool/src/util.rs` lines 42–45, the code explicitly acknowledges the mismatch but proceeds with the size-only check:
```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

`FeeRate` is defined as shannons per kilo-weight (`KW = 1000`), so `fee(weight)` computes `rate * weight / 1000`: [2](#0-1) 

**Transaction weight is `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`:** [3](#0-2) 

For a transaction at `max_tx_verify_cycles = 70,000,000`: `weight = max(tx_size, 70_000_000 × 0.000_170_571_4) = max(tx_size, 11_940)`. For a 200-byte transaction this is ~60× larger than `tx_size`.

**`check_tx_fee` is called before verification (cycles unknown):**

In `_process_tx`, `pre_check` (which calls `check_tx_fee`) runs before `verify_rtx`. After `verify_rtx` returns `verified.cycles`, there is **no second fee rate check** using the actual weight: [4](#0-3) 

The entry is created directly with `TxEntry::new(rtx, verified.cycles, fee, tx_size)` and submitted without re-checking the fee rate against the actual weight.

**All other pool subsystems use weight-based fee rate:**

- Pool sorting/eviction (`TxEntry::fee_rate`): uses `get_transaction_weight(self.size, self.cycles)` [5](#0-4) 
- Fee rate statistics RPC: uses `get_transaction_weight(*size, cycles)` [6](#0-5) 
- `estimate_fee_rate` RPC: returns weight-based fee rate via `entry.inner.fee_rate()` [7](#0-6) 

**Exploit flow:**
1. Attacker deploys (or reuses) a script cell that consumes near-maximum cycles with minimal witness data.
2. Attacker crafts a transaction: serialized size ~200 bytes, cycles ~70,000,000, fee = 200 shannons.
3. `check_tx_fee`: `min_fee = 1000 × 200 / 1000 = 200 shannons` → **PASSES**.
4. `verify_rtx` runs full script verification, consuming ~70M cycles of CPU.
5. Entry enters pool with effective weight-based fee rate of `200 × 1000 / 11940 ≈ 16 shannons/kW` — ~62× below `min_fee_rate`.
6. Miners deprioritize/never mine this transaction; it persists until pool eviction.
7. Attacker repeats, continuously triggering full verification CPU cost at ~60× discount.

## Impact Explanation

This maps to **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"** (High, 10001–15000 points). An attacker can exhaust tx-pool verification worker threads and pool memory at a fraction of the intended cost. Each submission forces a full script execution (up to `max_tx_verify_cycles` cycles) on the node. The pool's `limit_size` eviction uses weight-based fee rate, so these transactions are evicted first — but the attacker can continuously resubmit, keeping verification workers saturated. The `max_tx_pool_size` cap does not prevent the CPU exhaustion from repeated submissions.

## Likelihood Explanation

Reachable by any unprivileged user via the standard `send_transaction` RPC. No special privileges, keys, or majority hashpower are required. The attacker only needs valid UTXOs and a deployed heavy-computation script (or can reuse any existing one). The attack is cheap, repeatable, and requires no victim mistakes.

## Recommendation

Add a post-verification fee rate check in `_process_tx` after `verify_rtx` returns the actual cycles. At that point, `verified.cycles` is known and `get_transaction_weight(tx_size, verified.cycles)` can be computed to enforce `min_fee_rate` against the true weight:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

The pre-verification size-based check in `check_tx_fee` can remain as a cheap early filter, but the weight-based check must be enforced post-verification. Additionally, unify the `min_fee_rate` config comment and `TxPoolInfo` doc to use "shannons per kilo-weight" consistently. [8](#0-7) 

## Proof of Concept

**Config:** `min_fee_rate = 1_000` (default).

**Step 1:** Deploy a lock script that loops to consume ~70,000,000 cycles with a 1-byte witness argument.

**Step 2:** Create a transaction spending a UTXO locked by that script:
- Serialized size: ~200 bytes
- Fee: 200 shannons (just above `min_fee_rate.fee(200) = 200`)

**Step 3:** Submit via `send_transaction` RPC.

**Expected (broken) behavior:**
```
pre_check → check_tx_fee(tx_size=200):
  min_fee = 1000 * 200 / 1000 = 200 shannons ✓ PASSES

verify_rtx → cycles = 70_000_000 (full CPU cost incurred)

TxEntry created: fee_rate = FeeRate::calculate(200, max(200, 11940))
               = 200 * 1000 / 11940 ≈ 16 shannons/kW  (62× below min_fee_rate)
```

**Step 4:** Repeat in a loop. Each iteration forces full script verification on the node at ~60× below the intended minimum cost. A unit test can assert that after `verify_rtx`, `FeeRate::calculate(fee, get_transaction_weight(tx_size, verified.cycles)) >= min_fee_rate` — this assertion will fail with the current code. [9](#0-8) [10](#0-9)

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

**File:** util/types/src/core/fee_rate.rs (L3-37)
```rust
/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);

const KW: u64 = 1000;

impl FeeRate {
    /// Calculates the fee rate from a total fee and weight.
    pub fn calculate(fee: Capacity, weight: u64) -> Self {
        if weight == 0 {
            return FeeRate::zero();
        }
        FeeRate::from_u64(fee.as_u64().saturating_mul(KW) / weight)
    }

    /// Creates a fee rate from shannons per kilo-weight.
    pub const fn from_u64(fee_per_kw: u64) -> Self {
        FeeRate(fee_per_kw)
    }

    /// Returns the fee rate as shannons per kilo-weight.
    pub const fn as_u64(self) -> u64 {
        self.0
    }

    /// Creates a zero fee rate.
    pub const fn zero() -> Self {
        Self::from_u64(0)
    }

    /// Calculates the fee for a given weight.
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
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

**File:** tx-pool/src/process.rs (L269-316)
```rust
    pub(crate) async fn pre_check(
        &self,
        tx: &TransactionView,
    ) -> (Result<PreCheckedTx, Reject>, Arc<Snapshot>) {
        // Acquire read lock for cheap check
        let tx_size = tx.data().serialized_size_in_block();

        let (ret, snapshot) = self
            .with_tx_pool_read_lock(|tx_pool, snapshot| {
                let tip_hash = snapshot.tip_hash();

                // Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc.
                // It's also not possible for RBF, reject it directly
                check_txid_collision(tx_pool, tx)?;

                // Try normal path first, if double-spending check success we don't need RBF check
                // this make sure RBF won't introduce extra performance cost for hot path
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
                        if conflicts.is_none() {
                            // this mean one input's outpoint is dead, but there is no direct conflicted tx in tx_pool
                            // we should reject it directly and don't need to put it into conflicts pool
                            error!(
                                "{} is resolved as Dead, but there is no conflicted tx",
                                rtx.transaction.proposal_short_id()
                            );
                            return Err(Reject::Resolve(OutPointError::Dead(out)));
                        }
                        // we also return Ok here, so that the entry will be continue to be verified before submit
                        // we only want to put it into conflicts pool after the verification stage passed
                        // then we will double-check conflicts txs in `submit_entry`

                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(err) => Err(err),
                }
            })
            .await;
        (ret, snapshot)
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

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** rpc/src/util/fee_rate.rs (L103-105)
```rust
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
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
