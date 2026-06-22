### Title
`check_tx_fee` Uses `tx_size` as Weight While Actual Fee Rate Uses `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`, Allowing `min_fee_rate` Bypass - (`tx-pool/src/util.rs`)

---

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` calculates the minimum required fee using only `tx_size` as the weight denominator. However, the canonical weight function used everywhere else in the pool — `get_transaction_weight` — returns `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. When a transaction's cycle cost is high enough that `cycles × DEFAULT_BYTES_PER_CYCLES > tx_size`, the pre-admission fee check underestimates the required fee, allowing transactions whose **actual** fee rate is below `min_fee_rate` to enter the pool. This is the direct CKB analog of the Smilee `obtainedPremium` / actual-premium mismatch.

---

### Finding Description

**Step 1 — The pre-admission check uses size only.**

`check_tx_fee` (`tx-pool/src/util.rs:42-45`) explicitly acknowledges the inconsistency but proceeds with the cheaper calculation:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The gate condition is therefore: `fee >= min_fee_rate × tx_size`.

**Step 2 — The canonical weight function uses the maximum of two metrics.**

`get_transaction_weight` (`util/types/src/core/tx_pool.rs:298-303`) is the authoritative weight formula used for pool sorting, eviction, and fee-rate display:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` (`util/types/src/core/tx_pool.rs:279`).

**Step 3 — The admission flow runs the fee check *before* script execution.**

In `_process_tx` (`tx-pool/src/process.rs:715-751`):

```
pre_check()          ← check_tx_fee called here, cycles unknown
  └─ check_tx_fee(tx_pool, snapshot, rtx, tx_size)   // uses tx_size only

verify_rtx()         ← actual cycles determined here

TxEntry::new(rtx, verified.cycles, fee, tx_size)     // entry stores real cycles
```

After admission, `TxEntry::fee_rate()` (`tx-pool/src/component/entry.rs:115-118`) computes the real fee rate using `get_transaction_weight(self.size, self.cycles)` — the max formula — which can be far larger than `tx_size`.

**The mismatch:**

| Stage | Weight used | Formula |
|---|---|---|
| `check_tx_fee` (gate) | `tx_size` | size only |
| `TxEntry::fee_rate()` (actual) | `get_transaction_weight` | `max(size, cycles × k)` |

When `cycles × DEFAULT_BYTES_PER_CYCLES > tx_size`, the gate passes at `fee = min_fee_rate × tx_size`, but the stored fee rate is `fee / (cycles × DEFAULT_BYTES_PER_CYCLES)` — which is strictly below `min_fee_rate`.

**Concrete amplification example:**

- `tx_size = 1 000` bytes, `cycles = 70 000 000` (the `max_tx_verify_cycles` default)
- `cycles × DEFAULT_BYTES_PER_CYCLES ≈ 11 940` bytes
- Attacker pays fee for weight `1 000`, but occupies pool weight `11 940` — a **~12× amplification**

---

### Impact Explanation

An unprivileged tx-pool submitter (RPC `send_transaction` caller or P2P relay peer) can craft transactions with scripts that consume many cycles but have a small serialized size. By paying only `min_fee_rate × tx_size` shannons, the attacker's transactions pass the admission gate and enter the pool with actual fee rates far below `min_fee_rate`. This:

1. **Bypasses the spam-protection threshold** (`min_fee_rate`) that is the primary DoS guard for the tx-pool.
2. **Fills the pool with artificially cheap weight**, displacing or delaying legitimate transactions.
3. **Persists** as long as the attacker keeps submitting; the pool eviction mechanism (`EvictKey`) does use the real weight, but the attacker can continuously re-submit at low cost.

The disruption of core tx-pool functionality (admission gating) matches the target scope.

---

### Likelihood Explanation

- **Entry path**: any RPC caller or P2P relay peer — no privilege required.
- **Craft requirement**: a valid CKB script that consumes many cycles. Scripts up to `max_tx_verify_cycles = TWO_IN_TWO_OUT_CYCLES × 20 ≈ 70 000 000` cycles are accepted. Loop-heavy RISC-V scripts trivially achieve this.
- **Cost**: `min_fee_rate × tx_size` shannons per transaction — the cheapest possible admission cost.
- **No special conditions**: the vulnerability is always active whenever `cycles × DEFAULT_BYTES_PER_CYCLES > tx_size`, which is achievable by any script author.

---

### Recommendation

Move the fee-rate check to **after** script execution, where actual cycles are known, and compute `min_fee` using `get_transaction_weight(tx_size, verified.cycles)`:

```rust
// After verify_rtx returns verified.cycles:
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

For the relay path where `declared_cycles` is provided before execution, a preliminary check using `declared_cycles` can serve as an early rejection, with the authoritative check using actual cycles after verification.

---

### Proof of Concept

1. Author a CKB script that runs a tight loop consuming ~70 000 000 cycles. Deploy it on devnet.
2. Construct a transaction spending a cell locked by that script. Set `tx_size ≈ 1 000` bytes.
3. Set the transaction fee to exactly `min_fee_rate × 1 000` shannons (e.g., `1 000 shannons/KW × 1 000 bytes / 1 000 = 1 000 shannons`).
4. Submit via `send_transaction` RPC.
5. Observe: `check_tx_fee` passes (`fee >= min_fee_rate × tx_size`).
6. After script execution, `TxEntry::fee_rate()` returns `1 000 / 11 940 ≈ 83 shannons/KW` — well below `min_fee_rate = 1 000 shannons/KW`.
7. The transaction is now in the pool with an actual fee rate ~12× below the configured minimum. Repeat to fill the pool.

**Root cause files:**
- [1](#0-0) 
- [2](#0-1) 
- [3](#0-2) 
- [4](#0-3)

### Citations

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
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
