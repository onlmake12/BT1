### Title
Integer Division Truncation in `FeeRate::fee` Allows Zero-Fee Transactions to Bypass Minimum Fee Rate Check — (File: `util/types/src/core/fee_rate.rs`)

---

### Summary

`FeeRate::fee` computes the minimum required fee via integer division `(min_fee_rate * tx_size) / 1000`. When `min_fee_rate * tx_size < 1000`, the result truncates to zero shannons. The `check_tx_fee` function in the tx-pool then compares the actual transaction fee against this zero minimum, so any transaction — including a zero-fee one — passes the minimum fee rate gate.

---

### Finding Description

In `util/types/src/core/fee_rate.rs`, `FeeRate::fee` is defined as:

```rust
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;   // KW = 1000
    Capacity::shannons(fee)
}
``` [1](#0-0) 

`KW` is the constant 1000 (shannons per kilo-weight). The division is plain integer division, so any product `min_fee_rate * tx_size` that is less than 1000 truncates to zero. [2](#0-1) 

In `tx-pool/src/util.rs`, `check_tx_fee` calls this function to derive the minimum required fee and then gates admission:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
// reject txs which fee lower than min fee rate
if fee < min_fee {
    let reject = Reject::LowFeeRate(...);
    return Err(reject);
}
``` [3](#0-2) 

When `min_fee` truncates to `Capacity::shannons(0)`, the guard `fee < min_fee` becomes `fee < 0`, which is structurally impossible for a `Capacity` value. Every transaction — including one with zero fee — passes the check.

This is the direct analog of the reported bug: a value conversion (debt → base token in the original; fee → minimum-fee shannons in CKB) rounds down to zero, causing a guard that should reject the operation to silently pass.

---

### Impact Explanation

Any RPC caller invoking `send_transaction` or any relay peer forwarding a transaction can submit zero-fee transactions that are accepted into the mempool on nodes where `min_fee_rate * tx_size < 1000`. This enables:

- **Mempool spam**: An attacker floods the pool with zero-fee transactions at no cost, consuming the `max_tx_pool_size` budget.
- **Fee-paying transaction displacement**: When the pool is full, the eviction logic (`EvictKey` / `AncestorsScoreSortKey`) deprioritises low-fee-rate entries, but zero-fee entries that were admitted without rejection still occupy pool slots until evicted. [4](#0-3) 

---

### Likelihood Explanation

With the default `min_fee_rate = 1000` shannons/KW, the truncation to zero requires `tx_size < 1` byte, which is impossible for any valid CKB transaction. The default configuration is therefore safe. [5](#0-4) 

However, `min_fee_rate` is a user-configurable field: [6](#0-5) 

Nodes that lower `min_fee_rate` — a supported and documented option — become vulnerable. For example:

| `min_fee_rate` (shannons/KW) | `tx_size` threshold for `min_fee = 0` |
|---|---|
| 100 | < 10 bytes |
| 10 | < 100 bytes |
| 9 | < 112 bytes (covers many real transactions) |
| 1 | < 1000 bytes (covers virtually all transactions) |

A typical minimal CKB transaction serialises to roughly 100–300 bytes, so any node with `min_fee_rate ≤ 9` shannons/KW is fully exposed to zero-fee admission for common transaction sizes.

---

### Recommendation

Replace the floor division in `FeeRate::fee` with ceiling division so that a non-zero fee rate always produces at least 1 shannon of minimum fee:

```rust
pub fn fee(self, weight: u64) -> Capacity {
    if self.0 == 0 {
        return Capacity::zero();
    }
    // ceiling division: ensures min_fee >= 1 whenever fee_rate > 0
    let fee = (self.0.saturating_mul(weight).saturating_add(KW - 1)) / KW;
    Capacity::shannons(fee)
}
``` [1](#0-0) 

Alternatively, add an explicit post-condition in `check_tx_fee`: if `min_fee_rate > 0` and `min_fee == 0`, set `min_fee = Capacity::one()` before the comparison. [7](#0-6) 

---

### Proof of Concept

**Setup**: node configured with `min_fee_rate = 9` shannons/KW (a non-default but supported value).

**Transaction**: size = 100 bytes, fee = 0 shannons.

**Current behaviour**:
```
min_fee = FeeRate(9).fee(100)
        = (9 * 100) / 1000
        = 900 / 1000
        = 0          ← truncated to zero
fee (0) < min_fee (0)  →  false  →  transaction ACCEPTED
```

**With ceiling division**:
```
min_fee = (9 * 100 + 999) / 1000
        = 1899 / 1000
        = 1          ← at least 1 shannon
fee (0) < min_fee (1)  →  true   →  transaction REJECTED
```

The attacker-controlled entry path is the `send_transaction` RPC endpoint or the relay protocol, both of which call `check_tx_fee` for every incoming transaction. [8](#0-7) [4](#0-3)

### Citations

**File:** util/types/src/core/fee_rate.rs (L7-7)
```rust
const KW: u64 = 1000;
```

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

**File:** util/app-config/src/legacy/tx_pool.rs (L10-10)
```rust
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
```

**File:** util/app-config/src/configs/tx_pool.rs (L14-16)
```rust
    /// txs with lower fee rate than this will not be relayed or be mined
    #[serde(with = "FeeRateDef")]
    pub min_fee_rate: FeeRate,
```
