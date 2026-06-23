### Title
Fee-Rate Admission Check Uses Byte-Size Instead of Weight, Allowing Cycle-Heavy Transactions to Bypass `min_fee_rate` — (`File: tx-pool/src/util.rs`)

---

### Summary

The tx-pool admission gate in `check_tx_fee` enforces `min_fee_rate` using only the serialized byte size of the transaction as the weight denominator. However, the canonical weight metric used everywhere else in the pool — eviction, ordering, and fee-rate reporting — is `get_transaction_weight(size, cycles) = max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. An unprivileged RPC caller or P2P peer can craft a transaction that is small in bytes but consumes the maximum allowed cycles, paying only the size-based minimum fee while its true weight-adjusted fee rate is up to ~49× below the configured floor. The node is forced to execute the expensive script, admit the transaction, and relay it to peers — all at a fraction of the intended cost.

---

### Finding Description

`FeeRate` is defined as **shannons per kilo-weight** (KW = 1000 weight units). [1](#0-0) 

The canonical weight of a transaction is:

```
weight = max(tx_size_bytes, cycles × DEFAULT_BYTES_PER_CYCLES)
``` [2](#0-1) 

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. [3](#0-2) 

Every post-admission fee-rate computation — `TxEntry::fee_rate()`, `AncestorsScoreSortKey`, `EvictKey` — uses this weight: [4](#0-3) 

But the **admission gate** in `check_tx_fee` ignores cycles entirely and passes raw `tx_size` as the weight:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [5](#0-4) 

`check_tx_fee` is called inside `pre_check`, **before** script execution, so the actual cycle count is not yet known: [6](#0-5) 

After verification the real cycle count is stored in the `TxEntry`, but **no second fee-rate check is performed**: [7](#0-6) 

The two metrics — bytes for admission, weight for everything else — are inconsistent, exactly mirroring the decimal-mismatch class in the reference report.

---

### Impact Explanation

With the default configuration (`min_fee_rate = 1000` shannons/KW, `max_tx_verify_cycles = 70_000_000`): [8](#0-7) 

A minimum-size transaction (~242 bytes) that consumes 70 M cycles has:

| Metric | Value |
|---|---|
| Byte size | 242 B |
| Cycle weight | `70 000 000 × 0.000_170_571_4 ≈ 11 940` |
| True weight | `max(242, 11 940) = 11 940` |
| Admission min-fee (size-based) | `1000 × 242 / 1000 = 242` shannons |
| Effective fee rate after admission | `242 × 1000 / 11 940 ≈ 20` shannons/KW |
| Underpayment ratio | **~49×** |

Concrete impacts:

1. **CPU exhaustion per transaction**: The node executes up to 70 M RISC-V cycles of attacker-controlled script while the attacker pays only 242 shannons — the cost of a 242-byte zero-cycle transaction.
2. **Pool pollution**: Admitted transactions carry an effective fee rate of ~20 shannons/KW, far below the 1000 shannons/KW floor, displacing legitimate transactions from the pool.
3. **Relay amplification**: The node relays the transaction to peers via the P2P layer; each peer independently verifies the expensive script, multiplying the CPU cost across the network.

---

### Likelihood Explanation

**High.** The attack requires no privileged access. Any user who can call `send_transaction` over the public JSON-RPC endpoint, or any peer who can relay a transaction via the P2P protocol, can trigger this path. Crafting a compact RISC-V script that runs a tight loop consuming close to `max_tx_verify_cycles` is straightforward. The attacker needs only a valid UTXO to spend and 242 shannons of fee per transaction. [9](#0-8) 

---

### Recommendation

Replace the size-only minimum-fee check in `check_tx_fee` with a weight-based check that accounts for declared cycles. Because cycles are not yet verified at pre-check time, use the caller-declared cycle count (already available as `declared_cycles` in `_process_tx`) as a conservative upper bound:

```rust
// In check_tx_fee, accept declared_cycles as an additional parameter:
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
    declared_cycles: Cycle,   // <-- add this
) -> Result<Capacity, Reject> {
    let fee = /* ... as before ... */;
    let weight = get_transaction_weight(tx_size, declared_cycles);
    let min_fee = tx_pool.config.min_fee_rate.fee(weight);
    if fee < min_fee {
        return Err(Reject::LowFeeRate(...));
    }
    Ok(fee)
}
```

If `declared_cycles` is absent (relay path without declaration), fall back to `max_tx_verify_cycles` as the conservative bound. This closes the gap between the admission check and the post-admission fee-rate accounting without requiring a second verification pass.

---

### Proof of Concept

**Setup**: Node running with default config (`min_fee_rate = 1000`, `max_tx_verify_cycles = 70_000_000`).

**Step 1 — Craft the script**: Write a CKB-VM RISC-V program that executes a tight loop consuming exactly 69,999,999 cycles (just under the limit). The binary can be as small as ~100 bytes.

**Step 2 — Build the transaction**: Construct a transaction spending a live cell, attaching the loop script as a type script. The serialized transaction size will be ~242 bytes.

**Step 3 — Set fee to 242 shannons**: This satisfies the size-based check: `1000 × 242 / 1000 = 242`.

**Step 4 — Submit**:
```json
{"method": "send_transaction", "params": [<tx>, null]}
```

**Observed behavior**:
- `check_tx_fee` passes: `fee (242) >= min_fee (242)` ✓
- `verify_rtx` executes 70 M cycles of RISC-V — full CPU cost paid by the node
- Transaction admitted with effective fee rate `242 × 1000 / 11940 ≈ 20` shannons/KW — 49× below the configured minimum
- Transaction relayed to all connected peers, each repeating the 70 M-cycle verification

**Repeat**: Submit N such transactions. Each costs the attacker 242 shannons; each costs the node (and each peer) ~70 M cycles of CPU. The pool fills with transactions that will be evicted last only if the pool overflows, but the CPU cost of verification is already sunk. [10](#0-9) [11](#0-10) [2](#0-1)

### Citations

**File:** util/types/src/core/fee_rate.rs (L3-7)
```rust
/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);

const KW: u64 = 1000;
```

**File:** util/types/src/core/tx_pool.rs (L279-279)
```rust
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

**File:** tx-pool/src/process.rs (L715-717)
```rust
        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);
```

**File:** tx-pool/src/process.rs (L751-751)
```rust
        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
```

**File:** resource/ckb.toml (L212-215)
```text
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
max_tx_verify_cycles = 70_000_000
```

**File:** rpc/src/module/pool.rs (L612-634)
```rust
    fn send_transaction(
        &self,
        tx: Transaction,
        outputs_validator: Option<OutputsValidator>,
    ) -> Result<H256> {
        let tx: packed::Transaction = tx.into();
        let tx: core::TransactionView = tx.into_view();

        self.check_output_validator(outputs_validator, &tx)?;

        let tx_pool = self.shared.tx_pool_controller();
        let submit_tx = tx_pool.submit_local_tx(tx.clone());

        if let Err(e) = submit_tx {
            error!("Send submit_tx request error {}", e);
            return Err(RPCError::ckb_internal_error(e));
        }

        let tx_hash = tx.hash();
        match submit_tx.unwrap() {
            Ok(_) => Ok(tx_hash.into()),
            Err(reject) => Err(RPCError::from_submit_transaction_reject(&reject)),
        }
```
