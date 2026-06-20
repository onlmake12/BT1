### Title
Minimum Fee Check Uses Only Serialized Size, Ignoring Cycle-Based Weight — Transactions with High Cycles Bypass the Effective Fee Rate Floor - (File: `tx-pool/src/util.rs`)

---

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size. However, the actual transaction weight used for pool sorting and block assembly is `max(size, cycles * DEFAULT_BYTES_PER_CYCLES)`. A transaction with high cycle consumption but small serialized size passes the fee gate with a fee that is far below what the weight-based minimum fee rate would require. After script execution the real cycles are recorded in the `TxEntry`, but no second fee check is performed against the full weight. The transaction therefore enters the pool with an effective fee rate that can be orders of magnitude below `min_fee_rate`.

---

### Finding Description

**Root cause — `tx-pool/src/util.rs`, `check_tx_fee`:**

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);   // ← size only
``` [1](#0-0) 

The weight function used everywhere else in the pool is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,   // 0.000_170_571_4
    )
}
``` [2](#0-1) 

**Processing flow — `tx-pool/src/process.rs`, `_process_tx`:**

1. `pre_check` is called → `check_tx_fee` runs with `tx_size` only, cycles are unknown.
2. `verify_rtx` executes scripts → `verified.cycles` is now known.
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` is created with the real cycles.
4. `submit_entry` is called — **no second fee check against the full weight**. [3](#0-2) 

The `fee` value carried forward from step 1 is the raw capacity difference (inputs − outputs). It was only validated against the size-based floor. After step 2 the weight may be far larger, but the entry is admitted unconditionally.

**Numeric example** (default config: `min_fee_rate = 1000 shannons/KW`):

| Parameter | Value |
|---|---|
| `tx_size` | 200 bytes |
| `verified.cycles` | 70,000,000 (≈ `max_tx_verify_cycles`) |
| `cycles_weight` | 70 000 000 × 0.000_170_571_4 ≈ **11 940 bytes** |
| `weight` = `max(200, 11940)` | **11 940** |
| Fee required by size check | 200 shannons |
| Fee required by weight | 11 940 shannons |
| **Underpayment ratio** | **~60×** |

The attacker pays 200 shannons, passes `check_tx_fee`, and the entry is stored in the pool with `fee_rate() = FeeRate::calculate(200_shannons, 11940) ≈ 16 shannons/KW` — well below `min_fee_rate = 1000`. [4](#0-3) 

---

### Impact Explanation

1. **Minimum fee rate bypass for pool admission.** Any tx-pool submitter (local RPC caller or relayed remote peer) can craft a transaction that passes the admission gate while carrying an effective fee rate far below `min_fee_rate`. This is the direct analog of the Illuminate finding: a fee is computed on a subset of the resource consumed (size only), while the remainder (cycles) is processed without a proportional fee check.

2. **Verification resource exhaustion.** Each such transaction forces the node to execute up to `max_block_cycles` worth of CKB-VM cycles before the entry is admitted. Because the fee check does not gate on cycles, an attacker can submit a stream of high-cycle, low-fee transactions, saturating the async verify queue and consuming CPU.

3. **Pool space occupation without proportional fee.** The admitted entries occupy pool slots and are sorted near the bottom of the fee-rate order. They will not be mined but will persist until the expiry window (`expiry_hours`, default 12 h), crowding out legitimate transactions.

---

### Likelihood Explanation

- **Entry path is fully unprivileged.** Any node operator (local `send_transaction` RPC) or any connected peer (via `submit_remote_tx`) can trigger this path.
- **Crafting the exploit is straightforward.** A lock script that loops a fixed number of CKB-VM instructions produces arbitrarily high cycles with a small serialized script body. The attacker controls the cycle count precisely.
- **No special knowledge required.** The discrepancy is a direct consequence of the documented "cheap check" comment; the behavior is deterministic and reproducible.

---

### Recommendation

After `verify_rtx` returns `verified.cycles`, perform a second fee check against the full weight before admitting the entry:

```rust
// In _process_tx, after verified.cycles is known:
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

Alternatively, pass the declared/verified cycles into `check_tx_fee` so the single gate already uses the correct weight. For remote transactions where cycles are declared upfront, the declared value is already available at `pre_check` time and can be used immediately.

---

### Proof of Concept

1. Deploy a lock script whose body is ~50 bytes but whose execution iterates a tight loop consuming exactly `max_tx_verify_cycles` (70 000 000) cycles.
2. Construct a transaction spending a cell locked by that script. Serialized size ≈ 200 bytes.
3. Set `outputs_capacity = inputs_capacity − 200` (fee = 200 shannons).
4. Submit via `send_transaction` RPC.

**Expected (correct) behavior:** rejected with `LowFeeRate` because weight ≈ 11 940 and required fee ≈ 11 940 shannons.

**Actual behavior:** accepted into the pending pool. `fee_rate()` returns ≈ 16 shannons/KW, far below `min_fee_rate = 1000`. The node expends ~70 M VM cycles verifying the transaction before admitting it.

Repeat with hundreds of such transactions to exhaust the verify queue and fill the pool with permanently-unmined entries. [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```
