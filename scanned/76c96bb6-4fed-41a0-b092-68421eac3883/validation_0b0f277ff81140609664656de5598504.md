### Title
Reversed `since` Field Comparison Logic in `packed::CellInput` Ordering - (File: `util/gen-types/src/extension/rust_core_traits.rs`)

---

### Summary

The `Ord` implementation for `packed::CellInput` reverses the comparison of the `since` field. The code comment explicitly states the intended behavior ("smaller since values are prioritized and appear earlier in the ordering"), but the implementation does the opposite — making inputs with **smaller** `since` values compare as **Greater**, not Less. This is a direct analog to the external report's reversed `gte`/`lte` operator bug.

---

### Finding Description

In `util/gen-types/src/extension/rust_core_traits.rs`, the `Ord` implementation for `packed::CellInput` is:

```rust
impl ::core::cmp::Ord for packed::CellInput {
    #[inline]
    fn cmp(&self, other: &Self) -> ::core::cmp::Ordering {
        let previous_output_order = self.previous_output().cmp(&other.previous_output());
        if previous_output_order != ::core::cmp::Ordering::Equal {
            return previous_output_order;
        }

        // smaller since values are prioritized and appear earlier in the ordering
        other.since().cmp(&self.since())   // <-- reversed: self and other are swapped
    }
}
```

The comment states that smaller `since` values should appear **earlier** in the ordering (i.e., compare as `Less`). In Rust's `Ord` trait, "earlier" means `Ordering::Less`. For `self.since = 1000` and `other.since = 2000`, the correct expression is `self.since().cmp(&other.since())` → `1000.cmp(2000)` → `Less`. But the implementation uses `other.since().cmp(&self.since())` → `2000.cmp(1000)` → `Greater`, making the input with `since=1000` compare as **Greater** than the input with `since=2000`.

The unit test in `util/gen-types/src/extension/tests/rust_core_traits.rs` confirms this reversed behavior:

```rust
fn test_cellinput_cmp() {
    let a = CellInput::new_builder().since(1000u64).build();
    let b = CellInput::new_builder().since(2000u64).build();
    assert!(a > b);   // a.since < b.since, yet a is Greater — reversed
}
```

Every other packed type (`Uint32`, `Uint64`, `OutPoint`, `CellDep`, `Script`, `CellOutput`) uses the natural `self.field().cmp(&other.field())` ordering. `CellInput` is the sole exception, and the reversal contradicts its own comment. [1](#0-0) [2](#0-1) 

---

### Impact Explanation

The `since` field in a `CellInput` is a time-lock value that restricts when a transaction can be committed to the chain (RFC-0017). Any code that relies on `packed::CellInput` ordering — such as sorted collections (`BTreeSet`, `BTreeMap`), `min`/`max` operations, or binary-search-based lookups — will silently receive the opposite ordering for the `since` tiebreaker. Specifically:

- Inputs with **no time-lock** (`since = 0`) will sort as the **largest** (last) rather than the smallest (first), inverting the stated priority.
- Any future or existing consumer that sorts `CellInput` values expecting smaller `since` to come first will instead get larger `since` first, potentially causing incorrect transaction scheduling, incorrect deduplication behavior in sorted structures, or incorrect priority assignment.

The `Ord` implementation is in a foundational utility crate (`ckb-gen-types`) that is a dependency of the entire node. The bug is present every time the comparison is invoked, analogous to the external report's finding that the function "is incorrectly executed every time it is invoked." [3](#0-2) 

---

### Likelihood Explanation

The reversed comparison is present in production code and is exercised every time two `CellInput` values with the same `previous_output` but different `since` values are compared. Any transaction sender or RPC caller can craft inputs with differing `since` values to trigger this path. The bug is not gated behind any privilege check or special configuration. The test suite encodes the wrong behavior as the expected behavior, masking the defect from automated regression detection. [2](#0-1) 

---

### Recommendation

Replace the reversed comparison with the natural ordering that matches the stated intent:

```rust
impl ::core::cmp::Ord for packed::CellInput {
    #[inline]
    fn cmp(&self, other: &Self) -> ::core::cmp::Ordering {
        let previous_output_order = self.previous_output().cmp(&other.previous_output());
        if previous_output_order != ::core::cmp::Ordering::Equal {
            return previous_output_order;
        }

        // smaller since values are prioritized and appear earlier in the ordering
        self.since().cmp(&other.since())   // corrected
    }
}
```

Update the corresponding unit test to assert `a < b` (since `a.since=1000 < b.since=2000`). [4](#0-3) 

---

### Proof of Concept

1. Construct two `CellInput` values with the same `previous_output` (all-zero `OutPoint`) but different `since` values: `a.since = 1000`, `b.since = 2000`.
2. Call `a.cmp(&b)`.
3. **Expected** (per comment): `Ordering::Less` — smaller `since` appears earlier.
4. **Actual**: `other.since().cmp(&self.since())` = `2000u64.cmp(&1000u64)` = `Ordering::Greater`.
5. Any sorted collection (e.g., `BTreeSet<CellInput>`) will place `a` (since=1000) **after** `b` (since=2000), the opposite of the documented intent.

The test at line 113 (`assert!(a > b)`) passes today precisely because it was written to match the buggy implementation rather than the intended semantics. [5](#0-4) [6](#0-5)

### Citations

**File:** util/gen-types/src/extension/rust_core_traits.rs (L192-204)
```rust
impl ::core::cmp::Ord for packed::CellInput {
    #[inline]
    fn cmp(&self, other: &Self) -> ::core::cmp::Ordering {
        let previous_output_order = self.previous_output().cmp(&other.previous_output());
        if previous_output_order != ::core::cmp::Ordering::Equal {
            return previous_output_order;
        }

        // smaller since values are prioritized and appear earlier in the ordering
        other.since().cmp(&self.since())
    }
}
impl_cmp_partial_ord!(CellInput);
```

**File:** util/gen-types/src/extension/tests/rust_core_traits.rs (L109-114)
```rust
#[test]
fn test_cellinput_cmp() {
    let a = CellInput::new_builder().since(1000u64).build();
    let b = CellInput::new_builder().since(2000u64).build();
    assert!(a > b);
}
```
