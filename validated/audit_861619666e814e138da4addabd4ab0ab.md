Audit Report

## Title
Fee Rate Admission Check Uses Transaction Size Instead of Actual Weight, Allowing High-Cycle Transactions to Bypass Minimum Fee Rate — (`tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the serialized byte size of the transaction as the weight denominator. The actual weight used for pool scoring, eviction, and block assembly is `get_transaction_weight(tx_size, cycles) = max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. Because the fee check runs in `pre_check` before script execution (before cycles are known), and no second check is performed after `verify_rtx` determines actual cycles, a transaction with high cycles and small size can enter the pool with an actual fee rate far below `min_fee_rate`. This enables a CPU-and-memory griefing attack reachable via the P2P relay path without any special privileges.

## Finding Description

In `tx-pool/src/util.rs` lines 42–45, `check_tx_fee` computes the minimum required fee using only `tx_size`, with an explicit code comment acknowledging the imprecision:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The actual weight function used everywhere else (pool scoring, eviction, block assembly) is defined in `util/types/src/core/tx_pool.rs`:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

The processing pipeline in `_process_tx` (`tx-pool/src/process.rs`) is:

1. `pre_check` → `check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)` — fee check using **size only** (line 289)
2. `verify_rtx(...)` — script execution, actual cycles determined here (line 724)
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — entry created with actual cycles (line 751)
4. `submit_entry(...)` — added to pool (line 753)

There is **no second fee rate check** after step 2 using actual cycles. [3](#0-2) 

For relay transactions, `declared_cycles` is provided and must match `verified.cycles` (or the tx is rejected with `DeclaredWrongCycles`). However, the fee check in `pre_check` still uses only `tx_size` — so a relay transaction with `declared_cycles = actual_cycles` (a valid relay) still passes the fee check based on size alone and enters the pool with an actual fee rate far below `min_fee_rate`. [4](#0-3) 

For RPC submissions, `declared_cycles` is `None`, so `max_cycles = self.consensus.max_block_cycles()` (3,500,000,000 cycles on mainnet), making the attack window even larger than the 70M cycles cited in the claim. [5](#0-4) 

The `fee_rate()` method on `TxEntry` correctly uses `get_transaction_weight`, confirming the discrepancy between admission and scoring: [6](#0-5) 

**Concrete example (using mainnet defaults):**
- `tx_size = 1,000` bytes, `cycles = 70,000,000`, `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`
- `actual_weight = max(1000, 70_000_000 × 0.000_170_571_4) = 11,940`
- `min_fee_rate = 1,000 shannons/KB`
- Fee check threshold: `1,000 × 1,000 / 1,000 = 1,000 shannons`
- Actual minimum fee needed: `1,000 × 11,940 / 1,000 = 11,940 shannons`
- A transaction paying 1,001 shannons passes `check_tx_fee` but has an actual fee rate of ≈83 shannons/KB — ~12× below the minimum. [7](#0-6) 

## Impact Explanation

This is a **High** severity finding matching: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

An attacker can:
1. Craft transactions with small serialized size but high cycle consumption (up to `max_block_cycles`)
2. Pay only the size-proportional minimum fee (e.g., ~1,000 shannons for a 1,000-byte tx)
3. Force every receiving node to execute full script verification (up to 3.5B cycles on mainnet)
4. Have those transactions enter the pool with actual fee rates orders of magnitude below `min_fee_rate`, polluting pool ordering and consuming memory

The attack is repeatable: each submission forces a full script verification pass on every node that receives the relay. The CPU cost paid by nodes is proportional to `cycles`; the fee paid by the attacker is proportional to `tx_size`. This asymmetry is the core of the griefing vector. Pool eviction eventually removes these entries, but the verification CPU cost is already paid.

## Likelihood Explanation

- The P2P relay path is fully open to any network peer — no RPC access, keys, or privileges required
- The attacker needs only valid UTXOs to spend and pays a fee proportional to `tx_size` (not `cycles`)
- The attack is cheap and repeatable: each new UTXO enables another round
- The code comment at lines 42–44 explicitly acknowledges the imprecision, confirming this is a known gap rather than an intentional security design
- `max_block_cycles` on mainnet is 3,500,000,000 — far larger than the 70M `max_tx_verify_cycles` config value, widening the attack surface for RPC-submitted transactions [8](#0-7) 

## Recommendation

After `verify_rtx` returns `verified.cycles`, perform a second fee rate check using the true weight before calling `submit_entry`:

```rust
// After verify_rtx returns verified.cycles:
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let actual_min_fee = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < actual_min_fee {
    return Some((Err(Reject::LowFeeRate(tx_pool.config.min_fee_rate, actual_min_fee.as_u64(), fee.as_u64())), snapshot));
}
```

This check should be inserted in `_process_tx` in `tx-pool/src/process.rs` between lines 734 and 751, after `verified` is obtained and before `TxEntry::new` is called. For the relay path where `declared_cycles` is available before verification, `get_transaction_weight(tx_size, declared_cycles)` can also be used in `check_tx_fee` as a tighter pre-check to avoid wasting verification cycles on transactions that would fail the post-check.

## Proof of Concept

**Step 1:** Craft a CKB transaction with:
- Small serialized size (~1,000 bytes)
- A lock/type script that consumes close to `max_tx_verify_cycles` (70M) or `max_block_cycles` (3.5B) cycles
- Fee of `min_fee_rate × tx_size / 1000 + 1` shannons (just above the size-based threshold)

**Step 2:** Submit via P2P relay with `declared_cycles = actual_cycles` (required for relay validity).

**Step 3:** Observe that:
- The transaction passes `check_tx_fee` (size-based check passes)
- `verify_rtx` executes the full script, consuming the declared cycles
- The transaction is accepted into the pool
- `entry.fee_rate()` returns a value far below `min_fee_rate`

**Step 4:** Repeat with fresh UTXOs to continuously force expensive verification on all relay-connected nodes.

A unit test can be written in `tx-pool/src/tests/` that mocks a transaction with `tx_size = 1000` and `cycles = 70_000_000`, sets `min_fee_rate = 1_000`, pays `fee = 1_001` shannons, and asserts that the entry's `fee_rate()` is ~83 shannons/KB — confirming the discrepancy between admission threshold and actual fee rate. [9](#0-8)

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

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** resource/ckb.toml (L212-215)
```text
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
max_tx_verify_cycles = 70_000_000
```

**File:** spec/src/consensus.rs (L84-84)
```rust
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```
