### Title
Fee Rate Admission Check Uses Byte Size While Pool Ordering Uses Cycles-Weighted Size, Allowing Effective Fee Rate Bypass — (File: `tx-pool/src/util.rs`)

### Summary

The `check_tx_fee` function in `tx-pool/src/util.rs` gates pool admission using only the transaction's serialized byte size to compute the minimum required fee. However, the actual fee rate used for pool ordering, eviction, and block assembly uses `get_transaction_weight`, which takes the maximum of byte size and a cycles-derived weight. An unprivileged peer can craft a transaction with a small byte footprint but high declared cycles that passes the admission gate while carrying an effective fee rate far below `min_fee_rate`, consuming CPU verification resources without paying the appropriate fee.

### Finding Description

**Gate check (byte-size only)** in `tx-pool/src/util.rs`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee { return Err(Reject::LowFeeRate(...)); }
``` [1](#0-0) 

**Actual weight used for ordering and eviction** in `util/types/src/core/tx_pool.rs`:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

`TxEntry::fee_rate()` and `EvictKey` both use this weight-based calculation: [3](#0-2) [4](#0-3) 

The two-step admission flow in `_process_tx` makes the split explicit: `pre_check` (which calls `check_tx_fee` with byte size) runs first under a read lock, then full script verification runs with `declared_cycles` as the cycle cap: [5](#0-4) 

The `declared_cycles` value is peer-supplied and is accepted as long as it does not exceed `max_block_cycles`: [6](#0-5) 

**Exploit path:**

1. Attacker crafts a transaction with a small serialized byte size (e.g., 1 000 bytes) and a script that consumes close to `max_tx_verify_cycles` cycles (default ≈ 70 000 000 cycles, `TWO_IN_TWO_OUT_CYCLES * 20`).
2. Attacker pays a fee that satisfies `min_fee_rate.fee(tx_size)` — e.g., 1 shannon at 1 000 shannons/KW for 1 000 bytes.
3. Attacker relays the transaction with the correct `declared_cycles` value via the P2P relay protocol (`RelayTransactions`).
4. `check_tx_fee` passes: `min_fee = 1_000 * 1_000 / 1_000 = 1` shannon; fee ≥ 1 shannon. ✓
5. `verify_rtx` runs the script up to `declared_cycles`, consuming significant CPU.
6. `DeclaredWrongCycles` check passes because declared == actual.
7. The transaction is admitted. Its actual weight = `max(1_000, 70_000_000 × 0.000_170_571)` ≈ 11 940 bytes. Its actual fee rate ≈ `1 / 11.94` ≈ 0.08 shannons/KW — roughly 12× below `min_fee_rate`. [7](#0-6) 

### Impact Explanation

- **Fee rate floor bypass**: Transactions with effective fee rates well below `min_fee_rate` are admitted to the pool. The pool's stated economic invariant (reject anything below `min_fee_rate`) is violated.
- **CPU exhaustion**: Each such transaction forces the node to execute up to `max_tx_verify_cycles` cycles of script verification while the attacker pays only the byte-size-based minimum fee. Submitting many such transactions in parallel saturates the async verification worker pool.
- **Pool churn**: Admitted transactions are immediately candidates for eviction (lowest actual fee rate), but the verification CPU cost has already been paid. The attacker can continuously re-submit to sustain the load.

### Likelihood Explanation

Any unprivileged peer reachable via the P2P relay protocol can submit transactions. Crafting a high-cycle, small-byte transaction requires only writing a RISC-V script that loops; no privileged access, key material, or majority hashpower is needed. The relay path is the standard transaction propagation path and is always open.

### Recommendation

Replace the byte-size-only fee check in `check_tx_fee` with the same weight function used everywhere else in the pool:

```rust
// Instead of:
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);

// Use (requires declared_cycles to be threaded in):
let weight = get_transaction_weight(tx_size, declared_cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

For local (non-remote) transactions where `declared_cycles` is not yet known, use `max_block_cycles` as a conservative upper bound, or defer the fee check until after verification when actual cycles are known.

### Proof of Concept

```
1. Write a CKB lock script that loops for ~70_000_000 cycles.
2. Build a transaction:
   - 1 input cell (small lock args → small serialized size, e.g. 1 000 bytes)
   - 1 output cell
   - fee = ceil(min_fee_rate * 1_000 / 1_000) = 1 shannon (at default 1 000 shannons/KW)
3. Relay via RelayTransactions with declared_cycles = 70_000_000.
4. Node accepts: check_tx_fee sees tx_size=1000, min_fee=1, fee=1 → OK.
5. Node spends ~70M cycles of CPU verifying the script.
6. Transaction enters pool with actual fee_rate ≈ 0.08 shannons/KW (12× below min_fee_rate).
7. Repeat from step 3 to sustain CPU pressure.
``` [8](#0-7) [9](#0-8)

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

**File:** util/app-config/src/legacy/tx_pool.rs (L13-16)
```rust
// default max tx verify cycles
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
// default max ancestors count
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
```
