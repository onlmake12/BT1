### Title
Tx-Pool Admission Check Uses Serialized Size Only While Actual Fee Rate Uses Full Weight (Cycles-Inclusive), Allowing High-Cycle Transactions to Bypass Minimum Fee Rate Enforcement — (File: tx-pool/src/util.rs)

---

### Summary

The `check_tx_fee` function in `tx-pool/src/util.rs` computes the minimum required fee using only the transaction's serialized byte size, while the actual fee rate stored in every pool entry (`TxEntry::fee_rate()`) uses the full transaction *weight* — `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. This is the same class of inconsistency as the reported Lybra bug: one enforcement path uses a partial/incomplete value, the other uses the complete value, and an attacker can exploit the gap between them.

---

### Finding Description

**Path 1 — Admission gate (incomplete value: size only)**

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum fee threshold using only the serialized transaction size:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code itself acknowledges the incompleteness with an inline comment:

> "Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly" [1](#0-0) 

**Path 2 — Pool entry fee rate (complete value: weight = max(size, cycles))**

Once admitted, every `TxEntry` computes its actual fee rate using `get_transaction_weight`, which takes both size *and* cycles into account:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [2](#0-1) 

`get_transaction_weight` is defined as:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [3](#0-2) 

With `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` and the default `max_tx_verify_cycles = 70_000_000`, the maximum cycle-derived weight is:

```
70_000_000 × 0.000_170_571_4 ≈ 11,940 bytes
```

**The same inconsistency appears in the RBF extra-fee calculation**, which also uses size only:

```rust
let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
``` [4](#0-3) 

**Concrete divergence**

| Metric | Admission check (`check_tx_fee`) | Pool entry (`fee_rate()`) |
|---|---|---|
| Denominator | `tx_size` (bytes only) | `max(size, cycles × 0.000_170_571_4)` |
| For a 200-byte, 70 M-cycle tx | 200 | ≈ 11,940 |
| Min fee at 1000 shan/KB | 200 shannons | 11,940 shannons |
| Effective fee rate if fee = 201 shan | passes (201 ≥ 200) | ≈ 16.8 shan/KB (60× below minimum) |

---

### Impact Explanation

An unprivileged tx-pool submitter (RPC caller or relay peer) can craft a transaction that:

1. **Passes the admission gate** — fee ≥ `min_fee_rate × size / 1000` (trivially satisfied with a tiny fee).
2. **Has an actual fee rate far below `min_fee_rate`** — because the real weight is dominated by cycles, not bytes.

Consequences:
- The pool can be flooded with transactions whose effective fee rate is up to ~60× below the operator-configured minimum, defeating the spam-prevention intent of `min_fee_rate`.
- Each such transaction forces the node to run full script verification (up to 70 M cycles of CKB-VM execution), consuming significant CPU before the transaction is eventually evicted.
- The same bypass applies to the RBF `min_rbf_rate` extra-fee check, allowing a replacement transaction to pass the RBF fee-bump requirement while paying far less than the policy intends.

---

### Likelihood Explanation

The attack path is straightforward and requires no privileged access:

1. A script author deploys a lock or type script that consumes close to `max_tx_verify_cycles` (70 M) cycles — a trivial loop in CKB-VM RISC-V.
2. The attacker creates a transaction spending a cell locked by that script, keeping outputs minimal (small serialized size, e.g., 200 bytes).
3. The attacker sets the fee to just above `min_fee_rate × size / 1000` (e.g., 201 shannons).
4. The transaction is submitted via `send_transaction` RPC or relayed over P2P.

No special privileges, no majority hashpower, no social engineering required. Any RPC caller or relay peer can execute this.

---

### Recommendation

After script verification completes and actual cycles are known, perform a **second fee-rate check** using the full weight:

```rust
let weight = get_transaction_weight(tx_size, completed.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```

This mirrors the pattern used everywhere else in the pool (eviction, scoring, RBF) and closes the gap between the admission gate and the actual fee-rate accounting.

---

### Proof of Concept

```
1. Deploy a CKB-VM script that loops until it consumes ~70 M cycles.
2. Lock a cell with that script.
3. Build a transaction spending that cell:
      serialized_size ≈ 200 bytes
      cycles          ≈ 70_000_000
      fee             = 201 shannons   (just above min_fee_rate × 200 / 1000 = 200)
4. Submit via RPC: send_transaction(tx)
5. check_tx_fee passes: 201 ≥ 200  ✓
6. After verification, TxEntry::fee_rate() = 201 × 1000 / 11940 ≈ 16.8 shan/KB
      — 60× below the configured minimum of 1000 shan/KB
7. Transaction sits in the pool, consuming a verification slot and pool space,
   with an effective fee rate the operator explicitly intended to reject.
8. Repeat with many such transactions to exhaust the verify queue and pool capacity.
``` [5](#0-4) [2](#0-1) [6](#0-5) [7](#0-6)

### Citations

**File:** tx-pool/src/util.rs (L28-53)
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

**File:** tx-pool/src/pool.rs (L101-103)
```rust
    /// min_replace_fee = sum(replaced_txs.fee) + extra_rbf_fee
    fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
        let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
```
