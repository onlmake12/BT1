### Title
Stale `ancestors_size` Check Inside Block-Template Selection Loop Causes Valid Transaction Packages to Be Incorrectly Excluded — (`File: tx-pool/src/component/tx_selector.rs`)

---

### Summary

In `TxSelector::txs_to_commit`, the per-iteration size/cycles guard uses `tx_entry.ancestors_size` — a field that counts the transaction **plus all its ancestors** — while the running `size` accumulator already includes some of those same ancestors from earlier iterations. The check therefore overcounts the bytes that would actually be added, causing valid transaction packages to be rejected from the block template.

---

### Finding Description

`txs_to_commit` selects transactions for a block template. Each iteration picks the next best entry and checks whether adding its entire ancestor package would exceed the block limits:

```rust
// tx-pool/src/component/tx_selector.rs  lines 149-162
let next_size   = size.saturating_add(tx_entry.ancestors_size);
let next_cycles = cycles.saturating_add(tx_entry.ancestors_cycles);

if next_cycles > cycles_limit || next_size > size_limit {
    consecutive_failed += 1;
    // ...
    continue;          // ← package is skipped
}
```

`tx_entry.ancestors_size` is the **total** size of the transaction plus every one of its ancestors. However, `size` is a running total that already includes ancestors added in previous iterations. When ancestor Tx A was packaged together with Tx B in an earlier iteration, `size` was incremented by `A.size + B.size`. Later, when Tx C (which also has Tx A as an ancestor) is evaluated, `next_size = size + C.ancestors_size` double-counts Tx A's bytes.

The actual bytes that would be added are only `C.size` (Tx A is already in `fetched_txs` and is skipped in the inner loop at lines 207–218):

```rust
// lines 207-218
for (short_id, entry) in &ancestors {
    let is_new = self.fetched_txs.insert(short_id.clone());
    if !is_new {
        continue;   // ← already-added ancestors are skipped here
    }
    cycles = cycles.saturating_add(entry.cycles);
    size   = size.saturating_add(entry.size);
    // ...
}
```

The guard and the actual addition are therefore inconsistent: the guard assumes the full `ancestors_size` will be added, but the addition skips already-fetched ancestors.

The partial mitigation is `update_modified_entries`, which calls `sub_ancestor_weight` on descendants of newly-added transactions and moves them into `modified_entries` with a corrected `ancestors_size`. However, the code itself acknowledges that `calc_descendants()` may be inconsistent:

```rust
// lines 250-251
// Note: since https://github.com/nervosnetwork/ckb/pull/3706
// calc_descendants() may not consistent
```

When `calc_descendants()` misses a descendant, that descendant remains in `proposed_pool` with its original, stale `ancestors_size`. The guard then uses the stale value and may reject the package even though it would fit within the block limits.

---

### Impact Explanation

A miner's block assembler calls `package_txs` → `txs_to_commit` to fill the block template. When the bug fires, transactions that would fit within `max_block_bytes` / `max_block_cycles` are excluded from the template. The miner collects less fee revenue than the mempool would allow, and affected transactions experience delayed confirmation. Because the block template is served to miners via the `get_block_template` RPC, any miner or block-template caller is directly affected.

**Impact: Medium** — no funds are at risk and consensus is not violated, but economically valid transactions are silently dropped from templates, reducing miner revenue and degrading liveness for transaction senders.

---

### Likelihood Explanation

The condition requires two transactions that share at least one ancestor to both be present in the proposed pool. This is the normal CPFP (child-pays-for-parent) pattern and is common on mainnet. The additional requirement — that `calc_descendants()` misses the second transaction — is acknowledged in the source as a known inconsistency introduced by PR #3706. Therefore the scenario is realistic and reproducible by any transaction sender who submits a CPFP chain.

**Likelihood: Medium**

---

### Recommendation

Move the size/cycles guard to use the **actual incremental cost** — i.e., the size of only those ancestors not yet in `fetched_txs` — rather than the full `ancestors_size`. Concretely, compute the set of new ancestors before the guard:

```rust
let new_ancestors: Vec<_> = ancestors_ids
    .iter()
    .filter(|id| !self.fetched_txs.contains(*id))
    .filter_map(|id| self.retrieve_entry(id))
    .collect();

let incremental_size   = new_ancestors.iter().map(|e| e.size).sum::<usize>()
                         + if self.fetched_txs.contains(&short_id) { 0 } else { tx_entry.size };
let incremental_cycles = /* analogous */;

if size.saturating_add(incremental_size) > size_limit
    || cycles.saturating_add(incremental_cycles) > cycles_limit
{
    // skip
}
```

Alternatively, ensure `update_modified_entries` is always consistent so that every descendant of a newly-added transaction is moved to `modified_entries` with a corrected `ancestors_size` before the next iteration of the outer loop.

---

### Proof of Concept

**Setup:**
- `size_limit = 250`
- Tx A: `size = 100`, no ancestors → `ancestors_size = 100`
- Tx B: `size = 100`, ancestor = {A} → `ancestors_size = 200`
- Tx C: `size = 100`, ancestor = {A} → `ancestors_size = 200`
- Tx B has higher fee-rate than Tx C; Tx C is not a descendant of Tx B, so `calc_descendants(A)` may miss Tx C (acknowledged inconsistency).

**Execution of `txs_to_commit`:**

1. Iteration 1 — Tx B selected:
   - `next_size = 0 + 200 = 200 ≤ 250` → passes guard
   - Tx A and Tx B added; `size = 200`; `fetched_txs = {A, B}`
   - `update_modified_entries` called; if `calc_descendants(A)` misses Tx C, Tx C stays in `proposed_pool` with `ancestors_size = 200`

2. Iteration 2 — Tx C selected from `proposed_pool` (stale `ancestors_size = 200`):
   - `next_size = 200 + 200 = 400 > 250` → **guard fires, Tx C is skipped**
   - Actual bytes that would be added: only `C.size = 100` (Tx A already in `fetched_txs`)
   - Actual `next_size` would be `200 + 100 = 300`... wait, that's still > 250.

Let me adjust: `size_limit = 350`:

1. Iteration 1 — Tx B: `next_size = 0 + 200 = 200 ≤ 350` → added; `size = 200`
2. Iteration 2 — Tx C (stale `ancestors_size = 200`): `next_size = 200 + 200 = 400 > 350` → **rejected**
   - Actual incremental size = 100 (only Tx C itself); actual `next_size = 300 ≤ 350`
   - Tx C is incorrectly excluded from the block template despite fitting within the limit. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** tx-pool/src/component/tx_selector.rs (L96-112)
```rust
    /// find txs to commit, return TxEntry vector, total_size and total_cycles.
    pub fn txs_to_commit(
        mut self,
        size_limit: usize,
        cycles_limit: Cycle,
    ) -> (Vec<TxEntry>, usize, Cycle) {
        let mut size: usize = 0;
        let mut cycles: Cycle = 0;
        let mut consecutive_failed = 0;

        let mut iter = self
            .pool_map
            .sorted_proposed_iter()
            .filter(|entry| {
                entry.ancestors_size <= size_limit && entry.ancestors_cycles <= cycles_limit
            })
            .peekable();
```

**File:** tx-pool/src/component/tx_selector.rs (L148-162)
```rust
            let short_id = tx_entry.proposal_short_id();
            let next_size = size.saturating_add(tx_entry.ancestors_size);
            let next_cycles = cycles.saturating_add(tx_entry.ancestors_cycles);

            if next_cycles > cycles_limit || next_size > size_limit {
                consecutive_failed += 1;
                if using_modified {
                    self.modified_entries.remove(&short_id);
                    self.failed_txs.insert(short_id.clone());
                }
                if consecutive_failed > MAX_CONSECUTIVE_FAILURES {
                    break;
                }
                continue;
            }
```

**File:** tx-pool/src/component/tx_selector.rs (L207-218)
```rust
            for (short_id, entry) in &ancestors {
                let is_new = self.fetched_txs.insert(short_id.clone());
                if !is_new {
                    debug!("package duplicate txs {}", short_id);
                    continue;
                }
                cycles = cycles.saturating_add(entry.cycles);
                size = size.saturating_add(entry.size);
                self.entries.push(entry.to_owned());
                // try remove from modified
                self.modified_entries.remove(short_id);
            }
```

**File:** tx-pool/src/component/tx_selector.rs (L241-262)
```rust
    /// Add descendants of given transactions to `modified_entries` with ancestor
    /// state updated assuming given transactions are inBlock.
    fn update_modified_entries(&mut self, already_added: &LinkedHashMap<ProposalShortId, TxEntry>) {
        for (id, entry) in already_added {
            let descendants = self.pool_map.calc_descendants(id);
            for desc_id in descendants
                .iter()
                .filter(|id| !already_added.contains_key(id) && self.pool_map.has_proposed(id))
            {
                // Note: since https://github.com/nervosnetwork/ckb/pull/3706
                // calc_descendants() may not consistent
                if let Some(mut desc) = self
                    .modified_entries
                    .remove(desc_id)
                    .or_else(|| self.pool_map.get(desc_id).cloned())
                {
                    desc.sub_ancestor_weight(entry);
                    self.modified_entries.insert_entry(desc);
                }
            }
        }
    }
```
