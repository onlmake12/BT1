### Title
Inconsistent `script_len_range` Upper Bound in `get_cells_capacity` Returns Inflated Capacity — (`util/indexer/src/service.rs`)

---

### Summary

The `get_cells_capacity` RPC method applies the `script_len_range` filter with an **inclusive** upper bound (`> r1`) instead of the documented and intended **exclusive** upper bound (`>= r1`). This causes `get_cells_capacity` to count cells that `get_cells` with the identical filter would exclude, returning inflated capacity values to any RPC caller.

---

### Finding Description

Both `get_cells` and `get_cells_capacity` accept a `script_len_range: [u64; 2]` filter documented as `[inclusive, exclusive]`. The two implementations diverge in how they enforce the upper bound of this range.

**`get_cells`** — correct, exclusive upper bound:

```rust
// util/indexer/src/service.rs, line 303
if script_len < r0 || script_len >= r1 {
    return None;
}
```

**`get_cells_capacity`** — incorrect, inclusive upper bound:

```rust
// util/indexer/src/service.rs, line 780
if script_len < r0 || script_len > r1 {
    return None;
}
```

`get_cells` uses `>= r1` (excludes cells where `script_len == r1`), while `get_cells_capacity` uses `> r1` (retains cells where `script_len == r1`). The same one-character discrepancy appears for both `Lock` and `Type` script branches inside `get_cells_capacity`.

The documentation for both RPC methods explicitly states the range is `[inclusive, exclusive]`:

> `script_len_range: [u64; 2], filter cells by script len range, [inclusive, exclusive]` [1](#0-0) [2](#0-1) 

The documented contract is confirmed in the RPC README: [3](#0-2) 

---

### Impact Explanation

Any caller of `get_cells_capacity` that supplies a `script_len_range` filter receives a capacity total that includes cells whose script length equals the upper bound `r1`. Those same cells are excluded by `get_cells` under the identical filter. The result set of the two sibling RPCs is therefore inconsistent for any query where at least one live cell has `script_len == r1`.

Concrete consequences:
- A wallet or dApp computing available CKB capacity via `get_cells_capacity` will see a higher value than the cells actually returned by `get_cells`, leading to failed transaction construction (spending cells that do not satisfy the filter).
- A light client using `get_cells_capacity` to decide whether a user can afford an operation may approve operations that will ultimately fail on-chain.
- The discrepancy is silent — no error is returned, and the caller cannot distinguish a correct result from an inflated one without independently calling `get_cells` and summing capacities.

---

### Likelihood Explanation

The entry path is fully unprivileged: any JSON-RPC caller (local CLI user, dApp backend, light client) can reach `get_cells_capacity` with a `script_len_range` filter. No special role, key, or configuration is required. The bug is triggered whenever the upper bound of the range coincides with the script length of at least one live cell, which is a routine condition in practice (e.g., filtering for cells with no type script by setting `script_len_range: ["0x0", "0x1"]`).

---

### Recommendation

Change the two occurrences of `> r1` in the `get_cells_capacity` filter block to `>= r1`, matching `get_cells` and the documented `[inclusive, exclusive]` semantics:

```diff
- if script_len < r0 || script_len > r1 {
+ if script_len < r0 || script_len >= r1 {
```

Apply this fix to both the `Lock` branch (line 780) and the `Type` branch (line 790) inside `get_cells_capacity`. [2](#0-1) 

---

### Proof of Concept

Suppose two live cells exist:
- Cell A: `script_len = 33` (e.g., a type script with no args: 32-byte code_hash + 1-byte hash_type)
- Cell B: `script_len = 34` (e.g., a type script with 1-byte args)

RPC call: `get_cells_capacity` with `script_len_range: ["0x0", "0x21"]` (i.e., `[0, 33)`).

Expected (per documentation): only cells with `script_len < 33` are counted → Cell A and Cell B are both excluded.

Actual behavior in `get_cells_capacity`:
- Cell A: `33 < 0` is false, `33 > 33` is false → **Cell A is included** (wrong).
- Cell B: `34 < 0` is false, `34 > 33` is true → Cell B is excluded (correct).

Meanwhile `get_cells` with the same filter:
- Cell A: `33 < 0` is false, `33 >= 33` is true → Cell A is excluded (correct).
- Cell B: `34 < 0` is false, `34 >= 33` is true → Cell B is excluded (correct).

The capacity returned by `get_cells_capacity` is inflated by Cell A's capacity relative to what `get_cells` would return, and relative to the documented contract. [4](#0-3) [5](#0-4)

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

**File:** rpc/README.md (L3077-3077)
```markdown
        - script_len_range: [u64; 2], filter cells by script len range, [inclusive, exclusive]
```
