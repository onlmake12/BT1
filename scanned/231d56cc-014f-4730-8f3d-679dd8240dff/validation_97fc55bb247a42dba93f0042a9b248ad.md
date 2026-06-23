### Title
Inconsistent `script_len_range` Upper-Bound Semantics Between `get_cells` and `get_transactions` Indexer RPC Methods - (File: util/indexer/src/service.rs)

### Summary
The basic CKB indexer implements the `script_len_range` filter with contradictory boundary semantics across its two primary query methods. `get_cells` correctly enforces an exclusive upper bound (`[inclusive, exclusive)`), while `get_transactions` silently enforces an inclusive upper bound (`[inclusive, inclusive]`). Both methods document the range as `[inclusive, exclusive]`. An RPC caller relying on `get_transactions` with a `script_len_range` filter to enforce a script-length policy will receive a superset of the intended result set, silently including transactions that should have been excluded.

### Finding Description

The `script_len_range` filter parameter is documented uniformly across all indexer RPC methods as `[u64; 2], filter cells by script len range, [inclusive, exclusive]`.

In `get_cells` (`util/indexer/src/service.rs`), the upper bound is correctly treated as exclusive:

```rust
// get_cells path — lines 299-317
if let Some([r0, r1]) = filter_options.script_len_range {
    match filter_script_type {
        IndexerScriptType::Lock => {
            let script_len = extract_raw_data(&output.lock()).len();
            if script_len < r0 || script_len >= r1 {   // ✓ exclusive upper bound
                return None;
            }
        }
        IndexerScriptType::Type => {
            ...
            if script_len < r0 || script_len >= r1 {   // ✓ exclusive upper bound
                return None;
            }
        }
    }
}
```

In `get_transactions` (`util/indexer/src/service.rs`), the upper bound is treated as **inclusive**, contradicting the documented contract:

```rust
// get_transactions path — lines 776-794
if let Some([r0, r1]) = filter_options.script_len_range {
    match filter_script_type {
        IndexerScriptType::Lock => {
            let script_len = extract_raw_data(&output.lock()).len();
            if script_len < r0 || script_len > r1 {    // ✗ inclusive upper bound
                return None;
            }
        }
        IndexerScriptType::Type => {
            ...
            if script_len < r0 || script_len > r1 {    // ✗ inclusive upper bound
                return None;
            }
        }
    }
}
```

The single-character difference (`>=` vs `>`) means `get_transactions` admits one extra length value at the boundary.

### Impact Explanation

Any application that uses `get_transactions` with `script_len_range` to enforce a script-length policy will silently receive transactions that violate the intended filter. The canonical use case documented in the RPC README is filtering for cells with an **empty type script** using `script_len_range: ["0x0", "0x1"]`. Under `get_cells` this correctly returns only cells where `script_len == 0`. Under `get_transactions` the same range also returns transactions where `script_len == 1`, i.e., transactions that carry a non-empty type script. An application that relies on this filter to restrict which transactions it processes (e.g., a wallet or bridge that only wants to handle plain CKB transfers with no type script) will silently process transactions it should have excluded, potentially leading to incorrect accounting or unauthorized asset handling at the application layer.

### Likelihood Explanation

The inconsistency is reachable by any unprivileged RPC caller. No authentication, special role, or privileged access is required. The `get_transactions` RPC endpoint is a standard, publicly documented method. The `script_len_range` filter is explicitly shown in the official RPC README examples as the recommended way to filter by type-script presence. Any developer following the documentation and using `get_transactions` with this filter will be affected.

### Recommendation

**Short term:** Change the comparison operator in the `get_transactions` `script_len_range` filter from `>` to `>=` to match the documented `[inclusive, exclusive)` semantics and the behavior of `get_cells`:

```rust
// util/indexer/src/service.rs — get_transactions path
if script_len < r0 || script_len >= r1 {   // was: script_len > r1
    return None;
}
```

Apply the same audit to `get_cells_capacity` and any other indexer query paths that apply `script_len_range`.

**Long term:** Introduce a shared helper function for range-boundary checks so that all indexer query methods use a single, tested implementation, eliminating the possibility of per-method divergence.

### Proof of Concept

1. Index a chain that contains two transactions:
   - **Tx A**: output with an empty type script (`script_len == 0`)
   - **Tx B**: output with a minimal type script of exactly 1 byte serialized length (`script_len == 1`)

2. Call `get_cells` with `script_len_range: ["0x0", "0x1"]`:
   - Expected (documented): only Tx A's cell is returned.
   - Actual: only Tx A's cell is returned. ✓

3. Call `get_transactions` with the same `script_len_range: ["0x0", "0x1"]`:
   - Expected (documented): only Tx A is returned.
   - Actual: **both Tx A and Tx B are returned** because `script_len > r1` (i.e., `1 > 1`) is `false`, so Tx B passes the filter. ✗

The divergence is directly traceable to: [1](#0-0) 
(correct `>=` in `get_cells`) versus [2](#0-1) 
(incorrect `>` in `get_transactions`).

The documentation contract that both methods share is confirmed at: [3](#0-2) 
(`script_len_range: [u64; 2], filter cells by script len range, [inclusive, exclusive]`).

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
