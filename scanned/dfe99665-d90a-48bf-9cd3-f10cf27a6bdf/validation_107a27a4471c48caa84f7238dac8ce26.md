### Title
Incorrect Epoch Ordering Bound Check in `HeaderDigest::verify` Uses Raw Index Instead of Cross-Multiplied Fraction — (`File: util/types/src/utilities/merkle_mountain_range.rs`)

---

### Summary

`HeaderDigest::verify()` checks whether `start_epoch ≤ end_epoch` using a raw index comparison (`start_epoch.index() > end_epoch.index()`) when the epoch numbers are equal. This is the wrong value to compare: the correct check requires cross-multiplying indices by the opposing lengths (as the `Ord` implementation for `EpochNumberWithFraction` does). An unprivileged light-client sync peer can craft a `HeaderDigest` with the same epoch number but different lengths such that the raw index comparison passes while the true rational ordering is violated (`start_epoch > end_epoch`), causing the server to accept a structurally invalid digest.

---

### Finding Description

`HeaderDigest::verify()` performs three structural checks on a digest received over the light-client protocol. Check 2 (epoch ordering) reads:

```rust
if start_epoch != end_epoch
    && ((start_epoch_number > end_epoch_number)
        || (start_epoch_number == end_epoch_number
            && start_epoch.index() > end_epoch.index()))   // ← raw index comparison
``` [1](#0-0) 

The `Ord` implementation for `EpochNumberWithFraction`, however, correctly compares two epochs with the same epoch number by cross-multiplying their indices and lengths:

```rust
let a = self.index() * other.length();
let b = other.index() * self.length();
a.cmp(&b)
``` [2](#0-1) 

An `EpochNumberWithFraction` packs three independent fields — `number` (24 bits), `index` (16 bits), `length` (16 bits) — into a single `u64`: [3](#0-2) 

Because `index` and `length` are independent bit-fields, a `HeaderDigest` can legally encode `start_epoch` and `end_epoch` with the same `number` but **different** `length` values. In that case, the raw index comparison `start_epoch.index() > end_epoch.index()` gives the wrong answer.

**Concrete counter-example:**

| Field | `start_epoch` | `end_epoch` |
|---|---|---|
| number | 5 | 5 |
| index | 3 | 4 |
| length | 10 | 5 |
| rational value | 5 + 3/10 = **5.30** | 5 + 4/5 = **5.80** |

Raw check: `3 > 4` → **false** → passes (no error raised).
Correct check: `3 * 5 = 15` vs `4 * 10 = 40` → `15 < 40` → passes correctly.

**Second counter-example (the dangerous one — start > end but check misses it):**

| Field | `start_epoch` | `end_epoch` |
|---|---|---|
| number | 5 | 5 |
| index | 3 | 4 |
| length | 5 | 10 |
| rational value | 5 + 3/5 = **5.60** | 5 + 4/10 = **5.40** |

Raw check: `3 > 4` → **false** → passes (no error raised). ← **BUG: start > end is not detected**
Correct check: `3 * 10 = 30` vs `4 * 5 = 20` → `30 > 20` → error raised correctly.

The fix is simply to replace the manual decomposed comparison with the existing `Ord` implementation:

```rust
if start_epoch > end_epoch {
    // error
}
```

---

### Impact Explanation

The `HeaderDigest::verify()` method is the structural integrity gate for MMR proof nodes exchanged in the light-client protocol (`SendLastStateProof`, `SendBlocksProof`, `SendTransactionsProof`). A malformed digest with `start_epoch > end_epoch` (in rational terms) that passes this check can be embedded in a proof chain. Downstream consumers that rely on the ordering invariant (e.g., difficulty accumulation, epoch boundary reasoning) will operate on structurally inconsistent data. This can allow a malicious light-client peer to present a false proof of chain state that the server accepts as valid, undermining the security guarantees of the light-client protocol. [4](#0-3) 

---

### Likelihood Explanation

Any unprivileged peer speaking the light-client protocol can send a `HeaderDigest` with crafted `start_epoch`/`end_epoch` fields. No key material, privileged role, or majority hash power is required. The fields are independently settable bit-fields in a packed molecule struct. The triggering condition (same epoch number, different lengths, index ordering inverted in rational space) is trivially constructable.

---

### Recommendation

Replace the manual decomposed epoch ordering check with the existing `Ord` implementation, which already performs the correct cross-multiplication:

```rust
// 2. Check epochs.
let start_epoch: EpochNumberWithFraction = self.start_epoch().into();
let end_epoch: EpochNumberWithFraction = self.end_epoch().into();
if start_epoch > end_epoch {
    let errmsg = format!(
        "failed since the start epoch is bigger than the end ([{start_epoch:#},{end_epoch:#}])"
    );
    return Err(errmsg);
}
```

This delegates to the `Ord` impl at `util/types/src/core/extras.rs` lines 370–381, which correctly handles the case where `start_epoch.number() == end_epoch.number()` by comparing `self.index() * other.length()` against `other.index() * self.length()`. [2](#0-1) 

---

### Proof of Concept

Craft a `packed::HeaderDigest` with:
- `start_number = 100`, `end_number = 105`
- `start_epoch = EpochNumberWithFraction::new_unchecked(5, 3, 5)` → rational 5.60
- `end_epoch = EpochNumberWithFraction::new_unchecked(5, 4, 10)` → rational 5.40
- `start_compact_target != end_compact_target` (to avoid the same-epoch difficulty check)

Call `digest.verify()`. The current code evaluates `start_epoch_number (5) == end_epoch_number (5)` and `start_epoch.index() (3) > end_epoch.index() (4)` → `false`, so no error is returned. Yet `start_epoch` (5.60) is strictly greater than `end_epoch` (5.40), violating the ordering invariant. The correct check `start_epoch > end_epoch` (using `Ord`) evaluates `3 * 10 = 30 > 4 * 5 = 20` → `true`, correctly returning an error. [5](#0-4) [6](#0-5)

### Citations

**File:** util/types/src/utilities/merkle_mountain_range.rs (L60-123)
```rust
/// Trait for representing a header digest.
pub trait HeaderDigest {
    /// Verify the header digest
    fn verify(&self) -> Result<(), String>;
}

impl HeaderDigest for packed::HeaderDigest {
    /// Verify the MMR header digest
    fn verify(&self) -> Result<(), String> {
        // 1. Check block numbers.
        let start_number: BlockNumber = self.start_number().into();
        let end_number: BlockNumber = self.end_number().into();
        if start_number > end_number {
            let errmsg = format!(
                "failed since the start block number is bigger than the end ([{start_number},{end_number}])"
            );
            return Err(errmsg);
        }

        // 2. Check epochs.
        let start_epoch: EpochNumberWithFraction = self.start_epoch().into();
        let end_epoch: EpochNumberWithFraction = self.end_epoch().into();
        let start_epoch_number = start_epoch.number();
        let end_epoch_number = end_epoch.number();
        if start_epoch != end_epoch
            && ((start_epoch_number > end_epoch_number)
                || (start_epoch_number == end_epoch_number
                    && start_epoch.index() > end_epoch.index()))
        {
            let errmsg = format!(
                "failed since the start epoch is bigger than the end ([{start_epoch:#},{end_epoch:#}])"
            );
            return Err(errmsg);
        }

        // 3. Check difficulties when in the same epoch.
        let start_compact_target: u32 = self.start_compact_target().into();
        let end_compact_target: u32 = self.end_compact_target().into();
        let total_difficulty: U256 = self.total_difficulty().into();
        if start_epoch_number == end_epoch_number {
            if start_compact_target != end_compact_target {
                // In the same epoch, all compact targets should be same.
                let errmsg = format!(
                    "failed since the compact targets should be same during epochs ([{start_epoch:#},{end_epoch:#}])"
                );
                return Err(errmsg);
            } else {
                // Sum all blocks difficulties to check total difficulty.
                let blocks_count = end_number - start_number + 1;
                let block_difficulty = compact_to_difficulty(start_compact_target);
                let total_difficulty_calculated = block_difficulty * blocks_count;
                if total_difficulty != total_difficulty_calculated {
                    let errmsg = format!(
                        "failed since total difficulty is {total_difficulty} but the calculated is {total_difficulty_calculated} \
                        during epochs ([{start_epoch:#},{end_epoch:#}])"
                    );
                    return Err(errmsg);
                }
            }
        }

        Ok(())
    }
}
```

**File:** util/types/src/core/extras.rs (L370-381)
```rust
impl Ord for EpochNumberWithFraction {
    fn cmp(&self, other: &EpochNumberWithFraction) -> Ordering {
        match self.number().cmp(&other.number()) {
            ord @ Ordering::Less | ord @ Ordering::Greater => ord,
            _ => {
                let a = self.index() * other.length();
                let b = other.index() * self.length();
                a.cmp(&b)
            }
        }
    }
}
```

**File:** util/types/src/core/extras.rs (L383-425)
```rust
impl EpochNumberWithFraction {
    /// Bit offset for the epoch number field.
    pub const NUMBER_OFFSET: usize = 0;
    /// Number of bits for the epoch number field.
    pub const NUMBER_BITS: usize = 24;
    /// Maximum value for the epoch number field.
    pub const NUMBER_MAXIMUM_VALUE: u64 = (1u64 << Self::NUMBER_BITS);
    /// Bitmask for extracting the epoch number.
    pub const NUMBER_MASK: u64 = (Self::NUMBER_MAXIMUM_VALUE - 1);
    /// Bit offset for the index field.
    pub const INDEX_OFFSET: usize = Self::NUMBER_BITS;
    /// Number of bits for the index field.
    pub const INDEX_BITS: usize = 16;
    /// Maximum value for the index field.
    pub const INDEX_MAXIMUM_VALUE: u64 = (1u64 << Self::INDEX_BITS);
    /// Bitmask for extracting the index.
    pub const INDEX_MASK: u64 = (Self::INDEX_MAXIMUM_VALUE - 1);
    /// Bit offset for the length field.
    pub const LENGTH_OFFSET: usize = Self::NUMBER_BITS + Self::INDEX_BITS;
    /// Number of bits for the length field.
    pub const LENGTH_BITS: usize = 16;
    /// Maximum value for the length field.
    pub const LENGTH_MAXIMUM_VALUE: u64 = (1u64 << Self::LENGTH_BITS);
    /// Bitmask for extracting the length.
    pub const LENGTH_MASK: u64 = (Self::LENGTH_MAXIMUM_VALUE - 1);

    /// Creates a new epoch number with fraction.
    pub fn new(number: u64, index: u64, length: u64) -> EpochNumberWithFraction {
        debug_assert!(number < Self::NUMBER_MAXIMUM_VALUE);
        debug_assert!(index < Self::INDEX_MAXIMUM_VALUE);
        debug_assert!(length < Self::LENGTH_MAXIMUM_VALUE);
        debug_assert!(length > 0);
        Self::new_unchecked(number, index, length)
    }

    /// Creates a new epoch number with fraction without bounds checking.
    pub const fn new_unchecked(number: u64, index: u64, length: u64) -> Self {
        EpochNumberWithFraction(
            (length << Self::LENGTH_OFFSET)
                | (index << Self::INDEX_OFFSET)
                | (number << Self::NUMBER_OFFSET),
        )
    }
```
