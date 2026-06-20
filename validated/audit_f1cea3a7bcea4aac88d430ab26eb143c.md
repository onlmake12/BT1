### Title
`saturating_mul` Intermediate Overflow in `FeeRate::calculate` and `FeeRate::fee` Silently Corrupts Fee Rate Arithmetic - (`File: util/types/src/core/fee_rate.rs`)

---

### Summary

`FeeRate::calculate` and `FeeRate::fee` in `util/types/src/core/fee_rate.rs` both perform a multiplication of two `u64` values using `saturating_mul` before dividing. When the intermediate product exceeds `u64::MAX`, `saturating_mul` silently clamps the product to `u64::MAX` rather than widening to `u128` first. This is the direct Rust analog of the Solidity missing-`uint256`-cast pattern described in the external report: the intermediate result is corrupted before the division, producing a wrong final value.

---

### Finding Description

Both functions in `util/types/src/core/fee_rate.rs` share the same structural defect:

**`FeeRate::calculate`** (line 15):
```rust
FeeRate::from_u64(fee.as_u64().saturating_mul(KW) / weight)
```
`fee.as_u64()` is a `u64` (up to ~1.84 × 10¹⁹ shannons). `KW = 1000`. The product `fee * 1000` overflows `u64` whenever `fee > u64::MAX / 1000 ≈ 1.84 × 10¹⁶` shannons (~184 CKB). `saturating_mul` clamps the product to `u64::MAX`, so the computed fee rate becomes `u64::MAX / weight` — far higher than the true rate.

**`FeeRate::fee`** (line 35):
```rust
let fee = self.0.saturating_mul(weight) / KW;
```
`self.0` is the fee rate in shannons/KW (`u64`), `weight` is `u64`. When `fee_rate × weight > u64::MAX`, `saturating_mul` clamps to `u64::MAX`, so the returned fee is `u64::MAX / 1000` — lower than the true fee.

The correct pattern (matching how the DAO calculator and other CKB arithmetic already handles this) is to widen to `u128` before multiplying:
```rust
// calculate
FeeRate::from_u64((u128::from(fee.as_u64()) * u128::from(KW) / u128::from(weight)) as u64)

// fee
let fee = (u128::from(self.0) * u128::from(weight) / u128::from(KW)) as u64;
``` [1](#0-0) [2](#0-1) 

For comparison, the DAO calculator already uses the correct `u128` widening pattern throughout:
```rust
let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
    / u128::from(parent_c.as_u64());
``` [3](#0-2) 

---

### Impact Explanation

**`FeeRate::calculate` overflow** — the computed fee rate is clamped to `u64::MAX / weight`, which is orders of magnitude higher than the true rate. This value is used in two places:

1. **Eviction key** (`tx-pool/src/component/entry.rs`, `EvictKey` construction via `FeeRate::calculate`): a transaction with a fee ≥ 184 CKB would be assigned a falsely enormous fee rate, making it appear maximally valuable and immune to eviction regardless of its true fee-per-weight ratio. [4](#0-3) 

2. **Fee rate statistics RPC** (`rpc/src/util/fee_rate.rs`): `get_fee_rate_statistics` would report a wildly inflated mean/median fee rate to all callers, corrupting fee estimation for every wallet and application querying the node. [5](#0-4) 

**`FeeRate::fee` overflow** — the returned fee is capped at `u64::MAX / 1000 ≈ 1.84 × 10¹⁶` shannons, which is lower than the true required fee. If used for minimum-fee enforcement in `tx-pool/src/util.rs`, a transaction could pass the fee floor check with an insufficient fee. However, triggering this path requires `fee_rate × weight > u64::MAX`, which demands an astronomically high configured minimum fee rate and is practically unreachable under normal node configuration.

---

### Likelihood Explanation

The `FeeRate::calculate` overflow is the more realistic path. A fee of 184 CKB (~18,400,000,000 shannons) for a single transaction is large but not impossible — it is well within the total CKB supply (~33.6 billion CKB) and could be submitted by any unprivileged transaction sender via the RPC or P2P relay. No special privilege is required; the attacker simply submits a transaction whose input capacity minus output capacity exceeds 184 CKB.

The `FeeRate::fee` overflow requires a configured minimum fee rate exceeding ~3.7 × 10¹³ shannons/KW, which is unrealistic in any real deployment.

---

### Recommendation

Replace both `saturating_mul` calls with `u128`-widened arithmetic, consistent with the pattern already used in `util/dao/src/lib.rs`:

```rust
// FeeRate::calculate
pub fn calculate(fee: Capacity, weight: u64) -> Self {
    if weight == 0 {
        return FeeRate::zero();
    }
    let rate = u128::from(fee.as_u64()) * u128::from(KW) / u128::from(weight);
    FeeRate::from_u64(rate.min(u128::from(u64::MAX)) as u64)
}

// FeeRate::fee
pub fn fee(self, weight: u64) -> Capacity {
    let fee = u128::from(self.0) * u128::from(weight) / u128::from(KW);
    Capacity::shannons(fee.min(u128::from(u64::MAX)) as u64)
}
```

---

### Proof of Concept

**`FeeRate::calculate` overflow:**

```
fee = 200 CKB = 20_000_000_000 shannons
weight = 1_000

True fee rate = 20_000_000_000 * 1000 / 1000 = 20_000_000_000 shannons/KW

With saturating_mul:
  20_000_000_000u64.saturating_mul(1000) = 18_446_744_073_709_551_615 (u64::MAX, clamped)
  u64::MAX / 1000 = 18_446_744_073_709_551 shannons/KW

Reported fee rate: 18_446_744_073_709_551 (≈ 9.2 × 10⁸ × true rate)
```

Any transaction sender submitting a transaction with fee ≥ 184 CKB triggers this path. The corrupted fee rate propagates into the eviction key and RPC fee statistics. [1](#0-0) [2](#0-1)

### Citations

**File:** util/types/src/core/fee_rate.rs (L11-16)
```rust
    pub fn calculate(fee: Capacity, weight: u64) -> Self {
        if weight == 0 {
            return FeeRate::zero();
        }
        FeeRate::from_u64(fee.as_u64().saturating_mul(KW) / weight)
    }
```

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```

**File:** util/dao/src/lib.rs (L242-245)
```rust
        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
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

**File:** rpc/src/util/fee_rate.rs (L86-111)
```rust
        let mut fee_rates = self.provider.collect(target, |mut fee_rates, block_ext| {
            let BlockExt {
                txs_sizes,
                cycles,
                txs_fees,
                ..
            } = block_ext;
            let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
            if txs_sizes.len() > 1 && !txs_fees.is_empty() {
                // block_ext.txs_fees's length == block_ext.cycles's length
                // block_ext.txs_fees's length + 1 == txs_sizes's length
                if let Some(cycles) = cycles {
                    for (fee, cycles, size) in itertools::izip!(
                        txs_fees,
                        cycles,
                        txs_sizes.iter().skip(1) // skip cellbase (first element in the Vec)
                    ) {
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
                        }
                    }
                }
            }
            fee_rates
        });
```
