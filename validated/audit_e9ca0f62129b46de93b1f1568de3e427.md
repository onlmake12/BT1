### Title
Minimum Fee Rate Check Uses Only Serialized Size, Ignoring Cycles — Cycle-Heavy Transactions Bypass `min_fee_rate` Enforcement - (File: `tx-pool/src/util.rs`)

### Summary

The `check_tx_fee` function in `tx-pool/src/util.rs` enforces the minimum fee rate by computing the minimum required fee using only the transaction's serialized byte size as the weight denominator. However, the actual fee rate of a transaction in the pool is computed using `get_transaction_weight(tx_size, cycles)` = `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For cycle-heavy transactions, the actual weight is dominated by cycles, making the actual fee rate far lower than what `check_tx_fee` verified. An unprivileged transaction sender or relay peer can craft a transaction with a small serialized size but high script execution cycles, pass the fee gate with a trivially small fee, and inject below-minimum-fee-rate transactions into the tx-pool.

---

### Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` computes the minimum required fee as:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The code itself acknowledges the approximation with the comment: *"Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly."*

However, the actual fee rate of a `TxEntry` — used for pool prioritization, eviction, and block assembly — is computed via:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [2](#0-1) 

And `get_transaction_weight` is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [3](#0-2) 

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`.

The discrepancy: `check_tx_fee` uses `tx_size` as the weight denominator, but the true weight for a cycle-heavy transaction is `cycles * DEFAULT_BYTES_PER_CYCLES`, which can be orders of magnitude larger. The fee check is performed in `pre_check` before script execution (before cycles are known), and the entry is then stored with the actual verified cycles:

```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
// ... verify_rtx runs scripts, returns verified.cycles ...
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
``` [4](#0-3) 

**Concrete example:**
- `min_fee_rate = 1000 shannons/KW` (default)
- `tx_size = 200 bytes`, `cycles = 10,000,000`
- `weight = max(200, 10_000_000 × 0.000_170_571_4) = max(200, 1705) = 1705`
- `check_tx_fee` requires: `fee ≥ 1000 × 200 / 1000 = 200 shannons`
- Actual fee rate: `200 × 1000 / 1705 ≈ 117 shannons/KW` — **below `min_fee_rate`**

The transaction passes the gate but enters the pool with an effective fee rate of ~117 shannons/KW, far below the configured 1000 shannons/KW threshold.

---

### Impact Explanation

An attacker can inject an unbounded number of transactions into the tx-pool that violate the node's `min_fee_rate` policy. These transactions:

1. Consume tx-pool memory and CPU (script execution cycles are real work done by the node).
2. Displace legitimate transactions during pool eviction (eviction is based on actual fee rate, so these low-rate transactions are evicted last only if the pool is not full yet).
3. Degrade block assembly quality — the block assembler packages transactions by fee rate; injected low-rate transactions pollute the pool and can delay confirmation of legitimate transactions.
4. Bypass the spam-prevention mechanism that `min_fee_rate` is designed to enforce.

This is reachable via both the local `send_transaction` RPC and via the P2P relay path (`TransactionsProcess::execute` → `submit_remote_tx`). [5](#0-4) 

---

### Likelihood Explanation

Any unprivileged transaction sender or relay peer can exploit this. The attacker only needs to:
1. Craft a transaction with a lock/type script that consumes many cycles (e.g., a script with a tight computation loop).
2. Set the fee to just above `min_fee_rate × tx_size / 1000` shannons.
3. Submit via RPC or relay via P2P.

No special privileges, keys, or majority hashpower are required. The attack is cheap: the fee paid is proportional to `tx_size` (small), not to the actual cycles consumed.

---

### Recommendation

Replace the size-only weight approximation in `check_tx_fee` with the actual weight formula. Since cycles are not yet known at pre-check time for remote transactions, the node should use the **declared cycles** (provided by the relayer in the `RelayTransaction` message) as a conservative upper bound for the weight check:

```rust
// Use declared_cycles (if available) to compute a tighter minimum fee
let weight = get_transaction_weight(tx_size, declared_cycles.unwrap_or(0));
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

For local RPC submissions where no declared cycles exist, the node can either:
- Require the caller to declare cycles (as the relay protocol already does), or
- Perform a two-phase check: a size-based pre-check, then a cycle-aware post-check after verification, rejecting the entry if the actual fee rate falls below `min_fee_rate`.

---

### Proof of Concept

1. Configure a CKB node with `tx_pool.min_fee_rate = 1000` (shannons/KW).
2. Deploy a lock script that performs a tight computation loop consuming ~10,000,000 cycles.
3. Craft a transaction spending a cell locked by that script. The transaction serialized size is ~200 bytes.
4. Set the transaction fee to exactly 200 shannons (= `1000 × 200 / 1000`).
5. Submit via `send_transaction` RPC.
6. Observe: the transaction is accepted into the tx-pool (`check_tx_fee` passes because `200 ≥ 200`).
7. Query `get_transaction` and inspect the pool entry: the actual fee rate is `200 × 1000 / 1705 ≈ 117 shannons/KW`, well below the configured `min_fee_rate` of 1000 shannons/KW.
8. Repeat at scale to flood the pool with below-minimum-fee-rate transactions, bypassing the spam-prevention policy. [6](#0-5) [7](#0-6)

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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
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

**File:** tx-pool/src/process.rs (L720-751)
```rust
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

**File:** sync/src/relayer/transactions_process.rs (L63-93)
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

        shared_state.mark_as_known_txs(txs.iter().map(|(tx, _)| tx.hash()));

        let tx_pool = self.relayer.shared.shared().tx_pool_controller().clone();
        let peer = self.peer;
        self.relayer
            .shared
            .shared()
            .async_handle()
            .spawn(async move {
                for (tx, declared_cycles) in txs {
                    if let Err(e) = tx_pool
                        .submit_remote_tx(tx.clone(), declared_cycles, peer)
                        .await
                    {
                        error!("submit_tx error {}", e);
                    }
                }
            });
```
