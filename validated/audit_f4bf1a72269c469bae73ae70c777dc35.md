### Title
Fee Rate Admission Bypass via Size/Weight Unit Mismatch in `check_tx_fee` — (File: tx-pool/src/util.rs)

### Summary

The `check_tx_fee` function in `tx-pool/src/util.rs` computes the minimum required fee using raw transaction byte size as the weight denominator, while all subsequent fee-rate scoring in the tx-pool uses `get_transaction_weight(size, cycles)` — which can be orders of magnitude larger for cycle-heavy transactions. This unit mismatch allows any unprivileged transaction sender to submit transactions that pass the `min_fee_rate` admission gate while carrying an actual effective fee rate far below the configured threshold, bypassing the economic spam-protection floor.

### Finding Description

**Root cause — two incompatible weight units used at admission vs. scoring:**

`FeeRate` is defined as shannons per kilo-weight (KW = 1000):

```rust
// util/types/src/core/fee_rate.rs
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;   // KW = 1000
    Capacity::shannons(fee)
}
```

The canonical weight function accounts for both byte size and VM cycles:

```rust
// util/types/src/core/tx_pool.rs
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

But the admission check in `check_tx_fee` ignores cycles entirely and uses only byte size — a fact the code itself flags:

```rust
// tx-pool/src/util.rs  lines 42-45
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

After admission, `TxEntry::fee_rate()` uses the correct weight:

```rust
// tx-pool/src/component/entry.rs  lines 115-118
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

There is no second admission gate after script verification that re-checks the actual fee rate against `min_fee_rate`.

**Exploit path:**

An attacker crafts a transaction with a script that consumes close to `max_tx_verify_cycles` (default 70,000,000) but has a small serialized size (e.g., 200 bytes):

| Step | Value |
|---|---|
| `tx_size` | 200 bytes |
| `cycles` | 70,000,000 |
| Admission weight (size only) | 200 |
| `min_fee` at admission | `1000 × 200 / 1000 = 200 shannons` |
| Actual weight | `max(200, 70_000_000 × 0.000_170_571_4) ≈ 11,940` |
| Actual effective fee rate | `200 / 11940 × 1000 ≈ 16 shannons/KW` |
| Configured `min_fee_rate` | 1000 shannons/KW |

The transaction is admitted (200 shannons ≥ 200 shannons minimum), but its actual fee rate is ~16 shannons/KW — 62× below the configured floor.

The attacker entry path is the standard `send_transaction` RPC or P2P relay, both of which call `check_tx_fee` before script verification.

### Impact Explanation

- Any unprivileged RPC caller or P2P relay peer can submit transactions that bypass the `min_fee_rate` economic spam-protection floor.
- Such transactions are admitted to the mempool, consume verification resources (up to `max_tx_verify_cycles` per transaction), and occupy pool space.
- They are scored with a near-zero effective fee rate, making them nearly impossible to mine, but they persist until expiry (`expiry_hours`, default 12 h) or pool eviction.
- An attacker can systematically fill the mempool with cycle-heavy, low-fee-rate transactions, degrading pool quality and displacing legitimate transactions — a targeted mempool-pollution / resource-exhaustion attack.
- The attack is bounded by `max_tx_pool_size` (180 MB) and `max_tx_verify_cycles` (70 M cycles), but within those bounds the protection is effectively nullified.

### Likelihood Explanation

Medium. The attacker must author a CKB script that consumes many cycles while keeping the serialized transaction small. This is straightforward for any script author: a tight loop in CKB-VM (RISC-V) achieves high cycle consumption with minimal code bytes. No privileged access, leaked keys, or majority hashpower is required.

### Recommendation

1. After script verification completes and actual cycles are known, perform a second fee-rate check using `get_transaction_weight(tx_size, cycles)` before finalizing pool admission. Reject the transaction if its actual fee rate falls below `min_fee_rate`.
2. Alternatively, use a conservative cycle-based upper bound (e.g., `max_tx_verify_cycles`) in the pre-verification admission check so the size-only approximation cannot be exploited.
3. Remove or tighten the acknowledged approximation at `tx-pool/src/util.rs` lines 42–45.

### Proof of Concept

1. Author a CKB lock script consisting of a tight RISC-V loop that runs for ~70,000,000 cycles. The compiled binary is small (< 200 bytes of ELF).
2. Construct a transaction spending a cell locked by this script. The serialized transaction size is ~200 bytes.
3. Set the transaction fee to exactly `min_fee_rate.fee(200) = 200 shannons` (with default `min_fee_rate = 1000`).
4. Submit via `send_transaction` RPC.
5. Observe: `check_tx_fee` passes (200 ≥ 200). After verification, `TxEntry::fee_rate()` returns ~16 shannons/KW. The transaction sits in the pool with an effective fee rate 62× below the configured floor.
6. Repeat to fill the pool with such transactions, displacing higher-fee-rate legitimate transactions through eviction pressure.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
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

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** util/app-config/src/legacy/tx_pool.rs (L9-10)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
```
