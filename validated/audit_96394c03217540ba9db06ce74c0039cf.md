I have confirmed the finding. Let me verify the exact line numbers and context one more time before writing the report.

### Title
`get_cells_capacity` `script_len_range` Upper-Bound Uses Inclusive `>` Instead of Exclusive `>=`, Inflating Returned Capacity — (File: `util/indexer/src/service.rs`)

---

### Summary

The `get_cells_capacity` RPC handler applies the `script_len_range` filter with an **inclusive** upper bound (`> r1`) while the documented semantics, and the sibling `get_cells` implementation, both require an **exclusive** upper bound (`>= r1`). Any cell whose script length equals exactly `r1` is accepted by `get_cells_capacity` but rejected by `get_cells`, causing the two methods to diverge and `get_cells_capacity` to return an inflated total capacity.

---

### Finding Description

Both `get_cells` and `get_cells_capacity` accept an `IndexerSearchKey` with an optional `filter.script_len_range: [u64; 2]`. The RPC documentation and inline doc-comments explicitly declare the semantics as **`[inclusive, exclusive]`**.

`get_cells` implements this correctly:

```rust
// util/indexer/src/service.rs  line 303
if script_len < r0 || script_len >= r1 {   // >=  → exclusive upper bound ✓
    return None;
}
```

`get_cells_capacity` uses `>` instead of `>=`:

```rust
// util/indexer/src/service.rs  line 780
if script_len < r0 || script_len > r1 {    // >   → inclusive upper bound ✗
    return None;
}
```

The same off-by-one is present for both the `Lock` branch (line 780) and the `Type` branch (line 790) inside `get_cells_capacity`. [1](#0-0) [2](#0-1) 

The documented contract is confirmed in the RPC README and in the `IndexerRpc` trait doc-comment:

> `script_len_range: [u64; 2], filter cells by script len range, [inclusive, exclusive]` [3](#0-2) 

---

### Impact Explanation

For any query where at least one live cell has a script whose serialised length equals exactly `r1`:

- `get_cells` **excludes** that cell (correct per spec).
- `get_cells_capacity` **includes** that cell's capacity in its sum (incorrect).

The result is that `get_cells_capacity` returns a capacity value **higher than the true sum** of capacities of the cells that `get_cells` would enumerate for the identical query. Wallet software, dApps, and light-client tooling that call `get_cells_capacity` to determine spendable balance before constructing transactions will receive an inflated figure, potentially causing failed transactions or incorrect accounting decisions. The inconsistency is silently accepted — no error is returned — so callers have no indication the result is wrong.

---

### Likelihood Explanation

The trigger requires only a standard, unprivileged RPC call to `get_cells_capacity` with a `script_len_range` filter. No special permissions, keys, or network position are needed. The condition (`script_len == r1`) is easily satisfied on mainnet: a caller can choose `r1` to match any common script length (e.g., the 53-byte secp256k1-blake160 lock script), making the boundary cell trivially reachable. The bug is deterministic and reproducible for any node running the affected code.

---

### Recommendation

Change the two `>` comparisons in the `get_cells_capacity` filter closure to `>=`, matching the `get_cells` implementation and the documented `[inclusive, exclusive]` semantics:

```rust
// line 780 (Lock branch)
if script_len < r0 || script_len >= r1 {
    return None;
}

// line 790 (Type branch)
if script_len < r0 || script_len >= r1 {
    return None;
}
```

Add a regression test that calls both `get_cells` and `get_cells_capacity` with the same `script_len_range` and asserts that the capacity returned by `get_cells_capacity` equals the sum of capacities of the cells returned by `get_cells`.

---

### Proof of Concept

1. Index a chain containing a live cell whose lock script serialised length is exactly `N` bytes.
2. Call `get_cells` with `filter.script_len_range = [N-1, N]` (should include the cell, since `N-1 <= N < N`).
3. Call `get_cells_capacity` with the same filter.
4. Call `get_cells` with `filter.script_len_range = [N-1, N]` — cell is included (correct).
5. Call `get_cells` with `filter.script_len_range = [0, N]` — cell is **excluded** because `N >= N` (correct).
6. Call `get_cells_capacity` with `filter.script_len_range = [0, N]` — cell is **included** because `N > N` is false, so the guard does not fire (incorrect).

The capacity reported by step 6 exceeds the sum of capacities from step 5 by exactly the capacity of the boundary cell, demonstrating the inflation. [2](#0-1) [1](#0-0)

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

**File:** rpc/src/module/indexer.rs (L56-63)
```rust
    ///     - filter - filter cells by following conditions, all conditions are optional
    ///          - script: if search script type is lock, filter cells by type script prefix, and vice versa
    ///          - script_len_range: [u64; 2], filter cells by script len range, [inclusive, exclusive]
    ///          - output_data: filter cells by output data
    ///          - output_data_filter_mode: enum, prefix | exact | partial
    ///          - output_data_len_range: [u64; 2], filter cells by output data len range, [inclusive, exclusive]
    ///          - output_capacity_range: [u64; 2], filter cells by output capacity range, [inclusive, exclusive]
    ///          - block_range: [u64; 2], filter cells by block number range, [inclusive, exclusive]
```
