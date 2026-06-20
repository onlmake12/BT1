### Title
`get_cells_capacity` Uses Inclusive Upper Bound for `script_len_range` While `get_cells` Uses Exclusive Upper Bound, Causing Incorrect Capacity Accounting for Native CKB Cells - (File: `util/indexer/src/service.rs`)

---

### Summary

The `get_cells_capacity` RPC handler in `util/indexer/src/service.rs` applies the `script_len_range` filter with an **inclusive** upper bound (`> r1`), while the sibling `get_cells` handler applies it with an **exclusive** upper bound (`>= r1`). The documented contract for `script_len_range` is `[inclusive, exclusive]`. This inconsistency causes `get_cells_capacity` to silently include cells that `get_cells` would exclude, returning an inflated capacity sum to any RPC caller who uses `script_len_range` to isolate native CKB cells (cells with no type script).

---

### Finding Description

Both `get_cells` and `get_cells_capacity` accept a `script_len_range` filter parameter, documented as `[inclusive, exclusive]`. The `script_len` for a cell with no type script is `0`; for a cell with a type script it is at minimum `33` (32 bytes `code_hash` + 1 byte `hash_type` + 0 bytes `args`).

**In `get_cells`** (the correct implementation):

```rust
// util/indexer/src/service.rs  line 313
if script_len < r0 || script_len >= r1 {   // exclusive upper bound ✓
    return None;
}
```

**In `get_cells_capacity`** (the buggy implementation):

```rust
// util/indexer/src/service.rs  line 790
if script_len < r0 || script_len > r1 {    // inclusive upper bound ✗
    return None;
}
```

The same off-by-one exists for the `Lock` branch at line 780 vs line 303.

Consider a caller querying `script_len_range: [0, 33)` (r0=0, r1=33) to find cells with no type script:

| Function | Condition kept | Cells included |
|---|---|---|
| `get_cells` | `0 <= script_len < 33` | only script_len=0 (no type script) ✓ |
| `get_cells_capacity` | `0 <= script_len <= 33` | script_len=0 **and** script_len=33 (type script with empty args) ✗ |

`get_cells_capacity` silently adds the capacity of cells that have a type script with no args (script_len exactly equal to r1) to the returned sum.

---

### Impact Explanation

Any application that calls `get_cells_capacity` with a `script_len_range` filter to compute the total native CKB balance (cells without a type script) receives an **inflated** capacity figure. The returned value includes capacity from cells that carry a type script of exactly `r1` bytes. This is a data-integrity violation: the capacity sum reported by `get_cells_capacity` is inconsistent with the cell list returned by `get_cells` for the identical query parameters. Applications performing balance checks, wallet accounting, or capacity-based decisions based on this RPC will operate on incorrect data.

**Impact: Medium** — incorrect financial data returned to any RPC caller; no funds are directly stolen, but downstream logic relying on the capacity figure is silently corrupted.

---

### Likelihood Explanation

**Likelihood: High** — the RPC documentation explicitly demonstrates `script_len_range: [0, 1)` as the canonical way to filter for cells with an empty type script (native CKB cells). Any wallet, dApp, or tooling that uses `get_cells_capacity` with this filter pattern is affected. The pattern is the primary documented use-case for `script_len_range`.

---

### Recommendation

Change the upper-bound comparison in `get_cells_capacity` from `> r1` to `>= r1` for both the `Lock` and `Type` branches, matching the exclusive-upper-bound semantics used in `get_cells`:

```rust
// util/indexer/src/service.rs  lines 780 and 790
// Change:
if script_len < r0 || script_len > r1 {
// To:
if script_len < r0 || script_len >= r1 {
```

---

### Proof of Concept

**Query:** `get_cells_capacity` with `script_type: lock`, `script_len_range: [0, 33)`.

- `get_cells` returns only cells where the type script is absent (`script_len = 0`).
- `get_cells_capacity` returns the sum of capacities for cells where `script_len ∈ {0, 33}`, i.e., it also counts cells whose type script has exactly 0 args (33 raw bytes).

The discrepancy is directly observable by:
1. Calling `get_cells` with the same key and `script_len_range: [0, 33)` → collect the returned cells.
2. Calling `get_cells_capacity` with the same key and `script_len_range: [0, 33)` → note the capacity.
3. Summing the capacities of the cells from step 1 manually.
4. The two totals will differ by the aggregate capacity of all live cells whose type script has no args.

**Root cause lines:** [1](#0-0) 

**Correct implementation in `get_cells` for comparison:** [2](#0-1) 

**Documentation confirming `[inclusive, exclusive]` semantics:** [3](#0-2)

### Citations

**File:** util/indexer/src/service.rs (L299-317)
```rust
                if let Some([r0, r1]) = filter_options.script_len_range {
                    match filter_script_type {
                        IndexerScriptType::Lock => {
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
                        }
                    }
```

**File:** util/indexer/src/service.rs (L776-794)
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
```

**File:** rpc/README.md (L2305-2305)
```markdown
         - script_len_range: [u64; 2], filter cells by script len range, [inclusive, exclusive]
```
