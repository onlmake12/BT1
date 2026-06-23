### Title
Silent Truncating Cast in NervosDAO Withdrawal Calculation Causes Incorrect Maximum Withdraw — (File: `util/dao/src/lib.rs`)

### Summary

`DaoCalculator::calculate_maximum_withdraw` uses a silent truncating `as u64` cast to convert a `u128` intermediate result to `u64`. Every other analogous `u128`→`u64` conversion in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. When the intermediate value exceeds `u64::MAX`, the truncation silently produces a wrong (smaller) maximum-withdraw figure. Because this figure is used in consensus-critical transaction fee verification, a legitimate DAO withdrawal transaction is rejected and the depositor's funds become permanently unwithdrawable.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the interest-bearing withdrawal amount as:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast silently discards the high 64 bits if `withdraw_counted_capacity > u64::MAX`. Every other `u128`→`u64` narrowing in the same file is guarded:

```rust
// dao_field_with_current_epoch, line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;

// secondary_block_reward, line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) 

The formula is `counted_capacity × withdrawing_ar / deposit_ar`. Because `withdrawing_ar ≥ deposit_ar` always holds (the accumulate rate `ar` is monotonically non-decreasing), the result is always `≥ counted_capacity`. Overflow occurs when:

```
counted_capacity × (withdrawing_ar / deposit_ar) > u64::MAX
```

The AR starts at `DEFAULT_GENESIS_ACCUMULATE_RATE = 10^16` and grows by `parent_ar × g2 / parent_c` per block. [4](#0-3) 

The total CKB supply is ~3.36 × 10^18 shannons. `u64::MAX` is ~1.844 × 10^19. For a cell holding the entire supply, the AR ratio must exceed ~5.5× before overflow occurs. At the current secondary issuance rate (~1.344 billion CKB/year against a ~33.6 billion CKB base), this takes on the order of 40+ years. However, the condition is reachable in principle, and the code is demonstrably inconsistent with every other guarded cast in the same file.

---

### Impact Explanation

`calculate_maximum_withdraw` is called from `transaction_maximum_withdraw`, which is called from `transaction_fee` inside `DaoCalculator`. This is invoked during consensus-critical block and transaction verification in `verification/src/transaction_verifier.rs`. [5](#0-4) 

When the truncation fires, `withdraw_counted_capacity as u64` wraps to a value far smaller than the true result. The returned `withdraw_capacity` is therefore far smaller than the depositor's actual entitlement. The verifier then computes:

```rust
maximum_withdraw.safe_sub(outputs_capacity)
```

which underflows (returns `DaoError::Overflow`) because `outputs_capacity` (the correct withdrawal amount) exceeds the truncated `maximum_withdraw`. The withdrawal transaction is rejected by every honest node. Because the DAO contract enforces the same arithmetic, the depositor cannot construct any valid withdrawal transaction — their funds are permanently locked.

---

### Likelihood Explanation

The overflow requires `counted_capacity × ar_ratio > u64::MAX`. Given the total CKB supply cap and the slow growth of AR, this is not reachable in the near term. However:

1. The condition is mathematically reachable over a multi-decade horizon.
2. The code is already inconsistent — every other `u128`→`u64` conversion in the same file uses the checked `try_from` path, making this an unambiguous oversight rather than a deliberate design choice.
3. A transaction sender (the depositor) is the direct victim; no privileged role is required.

---

### Recommendation

Replace the silent truncating cast with the same checked conversion used elsewhere in the file:

```rust
// Before (unsafe):
Capacity::shannons(withdraw_counted_capacity as u64)

// After (consistent with the rest of the file):
Capacity::shannons(
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
)
``` [6](#0-5) 

---

### Proof of Concept

Construct a DAO deposit cell with `counted_capacity` near the total CKB supply and a `withdrawing_ar` sufficiently larger than `deposit_ar` such that:

```
counted_capacity * withdrawing_ar / deposit_ar > u64::MAX
```

For example (illustrative values):
- `counted_capacity = 3_360_000_000_000_000_000` shannons (≈ total supply)
- `deposit_ar     = 10_000_000_000_000_000` (genesis AR = 10^16)
- `withdrawing_ar = 55_000_000_000_000_000` (AR after ~40 years of secondary issuance)

```
withdraw_counted_capacity = 3_360_000_000_000_000_000 * 55_000_000_000_000_000
                          / 10_000_000_000_000_000
                        = 18_480_000_000_000_000_000   // > u64::MAX (18_446_744_073_709_551_615)
```

`withdraw_counted_capacity as u64` silently truncates to `33_255_926_290_384` — a value ~550,000× smaller than the correct result. The verifier rejects any withdrawal claiming the correct amount, and the depositor's funds are permanently locked. [7](#0-6) [8](#0-7)

### Citations

**File:** util/dao/src/lib.rs (L30-36)
```rust
    pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
        let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
        rtx.transaction
            .outputs_capacity()
            .and_then(|y| maximum_withdraw.safe_sub(y))
            .map_err(Into::into)
    }
```

**File:** util/dao/src/lib.rs (L149-158)
```rust
        let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
        let output_capacity: Capacity = output.capacity().into();
        let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

        Ok(withdraw_capacity)
```

**File:** util/dao/src/lib.rs (L202-204)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
```

**File:** util/dao/src/lib.rs (L256-258)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
```

**File:** util/dao/utils/src/lib.rs (L17-17)
```rust
const DEFAULT_GENESIS_ACCUMULATE_RATE: u64 = 10_000_000_000_000_000;
```

**File:** util/dao/utils/src/lib.rs (L104-110)
```rust
pub fn extract_dao_data(dao: Byte32) -> (u64, Capacity, Capacity, Capacity) {
    let data = dao.raw_data();
    let c = Capacity::shannons(LittleEndian::read_u64(&data[0..8]));
    let ar = LittleEndian::read_u64(&data[8..16]);
    let s = Capacity::shannons(LittleEndian::read_u64(&data[16..24]));
    let u = Capacity::shannons(LittleEndian::read_u64(&data[24..32]));
    (ar, c, s, u)
```
