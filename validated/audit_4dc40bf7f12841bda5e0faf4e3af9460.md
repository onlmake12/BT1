### Title
Wrong Comparison Operator in `script_len_range` Filter of `get_cells_capacity` — (`File: util/indexer/src/service.rs`)

### Summary

In `util/indexer/src/service.rs`, the `get_cells_capacity` RPC handler applies the `script_len_range` filter using `>` (inclusive upper bound) instead of `>=` (exclusive upper bound). This is inconsistent with the documented `[inclusive, exclusive]` semantics and with the `get_cells` handler, which correctly uses `>=`. As a result, `get_cells_capacity` returns an inflated capacity value that includes cells whose script length equals the upper bound `r1`, which should be excluded.

---

### Finding Description

The `script_len_range` filter is documented as `[inclusive, exclusive]` — i.e., a cell passes if `r0 <= script_len < r1`.

In `get_cells` (lines 303 and 313), the filter is applied correctly:

```rust
if script_len < r0 || script_len >= r1 {
    return None;
}
``` [1](#0-0) 

But in `get_cells_capacity` (lines 780 and 790), the upper bound uses `>` instead of `>=`:

```rust
if script_len < r0 || script_len > r1 {
    return None;
}
``` [2](#0-1) 

This means cells with `script_len == r1` pass the filter in `get_cells_capacity` but are correctly excluded in `get_cells`. The two RPC methods are semantically inconsistent for the same input.

---

### Impact Explanation

An RPC caller querying `get_cells_capacity` with a `filter.script_len_range` of `[r0, r1]` receives a total capacity that includes cells with `script_len == r1`. These cells are excluded by `get_cells` under the same filter. Applications that use `get_cells_capacity` to determine available CKB capacity (e.g., for transaction construction or balance checks) will receive an inflated value, potentially leading to incorrect financial decisions or failed transactions.

---

### Likelihood Explanation

Any unprivileged RPC caller can trigger this by calling `get_cells_capacity` with a `filter.script_len_range` parameter. The `IndexerSearchKeyFilter` struct exposes `script_len_range` as an optional public field, and the RPC is accessible without authentication. [3](#0-2) 

---

### Recommendation

Change the comparison in `get_cells_capacity` from `>` to `>=` for the upper bound of `script_len_range`, matching the behavior of `get_cells` and the documented `[inclusive, exclusive]` semantics:

```diff
- if script_len < r0 || script_len > r1 {
+ if script_len < r0 || script_len >= r1 {
```

Apply this fix to both the `Lock` branch (line 780) and the `Type` branch (line 790) of the `script_len_range` check inside `get_cells_capacity`. [4](#0-3) 

---

### Proof of Concept

1. Index a chain containing two cells:
   - Cell A: lock script with serialized length = 50 bytes
   - Cell B: lock script with serialized length = 60 bytes

2. Call `get_cells` with `filter.script_len_range = [40, 60]`:
   - Expected (exclusive upper bound): only Cell A (len=50) is returned. Cell B (len=60) is excluded.
   - Actual: Cell A returned. ✓

3. Call `get_cells_capacity` with the same `filter.script_len_range = [40, 60]`:
   - Expected: capacity of Cell A only.
   - Actual: capacity of Cell A **and** Cell B (len=60 passes `60 > 60` = false, so it is NOT excluded). ✗

The capacity returned by `get_cells_capacity` is higher than the sum of capacities of cells returned by `get_cells` for the same filter, demonstrating the inconsistency caused by the wrong operator. [2](#0-1) [1](#0-0)

### Citations

**File:** util/indexer/src/service.rs (L302-315)
```rust
                            let script_len = extract_raw_data(&output.lock()).len();
                            if script_len < r0 || script_len >= r1 {
                                return None;
                            }
                        }
                        IndexerScriptType::Type => {
                            let script_len = output
                                .type_()
                                .to_opt()
                                .map(|script| extract_raw_data(&script).len())
                                .unwrap_or_default();
                            if script_len < r0 || script_len >= r1 {
                                return None;
                            }
```

**File:** util/indexer/src/service.rs (L776-795)
```rust
                if let Some([r0, r1]) = filter_options.script_len_range {
                    match filter_script_type {
                        IndexerScriptType::Lock => {
                            let script_len = extract_raw_data(&output.lock()).len();
                            if script_len < r0 || script_len > r1 {
                                return None;
                            }
                        }
                        IndexerScriptType::Type => {
                            let script_len = output
                                .type_()
                                .to_opt()
                                .map(|script| extract_raw_data(&script).len())
                                .unwrap_or_default();
                            if script_len < r0 || script_len > r1 {
                                return None;
                            }
                        }
                    }
                }
```

**File:** util/jsonrpc-types/src/indexer.rs (L131-147)
```rust
#[derive(Deserialize, Default, JsonSchema)]
pub struct IndexerSearchKeyFilter {
    /// if search script type is lock, filter cells by type script prefix, and vice versa
    pub script: Option<Script>,
    /// filter cells by script len range
    pub script_len_range: Option<IndexerRange>,
    /// filter cells by output data
    pub output_data: Option<JsonBytes>,
    /// output data filter mode, optional default is `prefix`
    pub output_data_filter_mode: Option<IndexerSearchMode>,
    /// filter cells by output data len range
    pub output_data_len_range: Option<IndexerRange>,
    /// filter cells by output capacity range
    pub output_capacity_range: Option<IndexerRange>,
    /// filter cells by block number range
    pub block_range: Option<IndexerRange>,
}
```
