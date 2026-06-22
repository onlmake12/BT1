Now I have enough context to write up the finding. The inconsistency is clear:

- `get_cells` at line 303/313: `script_len >= r1` → exclusive upper bound
- `get_cells_capacity` at line 780/790: `script_len > r1` → inclusive upper bound (diverges)
- All other range filters in `get_cells_capacity` (lines 816, 823, 829) use `>= r1` (exclusive)

---

### Title
`get_cells_capacity` `script_len_range` upper-bound check is inclusive (`> r1`) while `get_cells` and every other range filter use exclusive (`>= r1`), causing the two APIs to silently disagree on which cells qualify - (`File: util/indexer/src/service.rs`)

### Summary
`IndexerService::get_cells_capacity` applies the `script_len_range` filter with an inclusive upper bound (`script_len > r1`), while `IndexerService::get_cells` applies the identical filter with an exclusive upper bound (`script_len >= r1`). Every other range filter inside `get_cells_capacity` (`output_data_len_range`, `output_capacity_range`, `block_range`) also uses the exclusive form. The result is that `get_cells_capacity` silently counts cells whose script length equals exactly `r1` (the range end), while `get_cells` with the same search key excludes those same cells. Any caller that uses `get_cells_capacity` to size a transaction and then `get_cells` to collect the inputs will observe a capacity shortfall and produce a transaction that is rejected by the node.

### Finding Description
In `util/indexer/src/service.rs`, the `get_cells` method filters cells by `script_len_range` as follows:

```rust
// get_cells – lines 303 / 313
if script_len < r0 || script_len >= r1 {   // exclusive upper bound ✓
    return None;
}
```

The `get_cells_capacity` method, which is a separate code path that reuses the same `FilterOptions` struct and the same `script_len_range` field, contains:

```rust
// get_cells_capacity – lines 780 / 790
if script_len < r0 || script_len > r1 {    // inclusive upper bound ✗
    return None;
}
```

The `>` vs `>=` difference means `get_cells_capacity` accepts cells where `script_len == r1`, while `get_cells` rejects them. Every other range guard inside `get_cells_capacity` uses the exclusive form:

```rust
// output_data_len_range  – line 816
output_data.len() >= r1
// output_capacity_range  – line 823
capacity >= r1
// block_range            – line 829
block_number >= r1
```

The `script_len_range` guard is the only one that deviates.

### Impact Explanation
An RPC caller (wallet, dApp, exchange) that:
1. Calls `get_cells_capacity` with a `script_len_range` whose end value `r1` is a script length that exists on-chain, and
2. Uses the returned capacity figure to decide how many CKBytes are available, then
3. Calls `get_cells` with the same search key to collect the actual inputs

will receive a capacity total from step 1 that is inflated by the capacity of all cells whose script length equals exactly `r1`. When the caller assembles a transaction using only the cells returned by `get_cells`, the inputs will be short of the amount the capacity check promised. The resulting transaction will fail validation and the caller wastes fees. In automated systems (e.g., batch-payment scripts, bridge relayers) this can cause repeated failed submissions until the discrepancy is diagnosed.

### Likelihood Explanation
`script_len_range` is a documented, supported filter parameter of the CKB indexer RPC. Any unprivileged RPC caller can trigger the divergence simply by supplying a `script_len_range` whose end value coincides with a script length present in the UTXO set. No special privileges, keys, or network position are required. The CKB mainnet UTXO set contains many cells with common script lengths (e.g., secp256k1 lock scripts), so the boundary value is routinely hit in practice.

### Recommendation
Change both occurrences of `> r1` in `get_cells_capacity` to `>= r1` to match the exclusive-end semantics used everywhere else:

```rust
// util/indexer/src/service.rs  lines ~780 and ~790
- if script_len < r0 || script_len > r1 {
+ if script_len < r0 || script_len >= r1 {
    return None;
}
```

A regression test should assert that `get_cells_capacity` and the sum of capacities from `get_cells` agree for a `script_len_range` whose end value exactly matches a cell's script length.

### Proof of Concept
Consider a chain state with two cells:
- Cell A: lock script serialized length = 53 bytes, capacity = 100 CKB
- Cell B: lock script serialized length = 54 bytes, capacity = 200 CKB

Query both APIs with `script_len_range = [0, 54]` (i.e., `r0 = 0`, `r1 = 54`):

**`get_cells`** (line 303): rejects Cell B because `54 >= 54` → returns only Cell A → total capacity = 100 CKB.

**`get_cells_capacity`** (line 780): keeps Cell B because `54 > 54` is false → sums both cells → reports 300 CKB.

A caller that trusts the 300 CKB figure and then collects inputs via `get_cells` only receives Cell A (100 CKB). Any transaction built to spend 300 CKB worth of outputs will be rejected by the node for insufficient input capacity. [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** util/indexer/src/service.rs (L815-832)
```rust
                if let Some([r0, r1]) = filter_options.output_data_len_range
                    && (output_data.len() < r0 || output_data.len() >= r1)
                {
                    return None;
                }

                if let Some([r0, r1]) = filter_options.output_capacity_range {
                    let capacity: core::Capacity = output.capacity().into();
                    if capacity < r0 || capacity >= r1 {
                        return None;
                    }
                }

                if let Some([r0, r1]) = filter_options.block_range
                    && (block_number < r0 || block_number >= r1)
                {
                    return None;
                }
```
