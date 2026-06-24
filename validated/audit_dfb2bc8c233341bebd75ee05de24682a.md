Audit Report

## Title
Overcounted `replace_count` in `check_rbf` Due to Non-Deduplicated Shared Descendants Causes False RBF Rejection — (File: `tx-pool/src/pool.rs`)

## Summary
In `TxPool::check_rbf`, `replace_count` is accumulated by summing `descendants.len() + 1` for each conflicting transaction independently inside a loop. Because CKB transactions support multiple inputs, a single descendant can be a child of two or more conflicting transactions simultaneously, causing it to be counted multiple times. This makes `replace_count` exceed `MAX_REPLACEMENT_CANDIDATES` (100) even when the true number of unique transactions to replace is within the limit, resulting in false rejection of legitimate RBF transactions.

## Finding Description
At `tx-pool/src/pool.rs` lines 613–624, `replace_count` is incremented per-conflict without any cross-conflict deduplication: [1](#0-0) 

`calc_descendants` at `tx-pool/src/component/links.rs` lines 82–84 returns a `HashSet<ProposalShortId>` of all transitive children of a single transaction: [2](#0-1) 

Because CKB transactions support multiple inputs, a descendant `TX_C` can legitimately spend outputs from two different conflicting transactions `TX_A` and `TX_B` simultaneously. In that case, `calc_descendants(TX_A)` and `calc_descendants(TX_B)` both return `{TX_C}`, and `replace_count` counts it twice. With 50 such shared descendants: `replace_count = (50+1) + (50+1) = 102 > 100`, triggering rejection even though only 52 unique transactions would actually be replaced.

`find_conflict_tx` correctly identifies both `TX_A` and `TX_B` as conflicts when the victim's transaction spends both their inputs: [3](#0-2) 

The existing guard (the `> MAX_REPLACEMENT_CANDIDATES` check at line 619) is the broken check itself — it fires on the inflated, non-deduplicated count. No other guard prevents this false rejection. Notably, `calculate_min_replace_fee` correctly deduplicates via a `HashMap` keyed by `id`, proving the pattern for the fix exists in the same file: [4](#0-3) 

## Impact Explanation
This is a targeted DoS on the RBF mechanism. An attacker can permanently block a victim's ability to fee-bump or replace specific in-pool transactions by constructing shared-descendant fan-in structures. Legitimate RBF transactions are incorrectly rejected with `"Tx conflict with too many txs"` even when the true unique replacement count is within the protocol limit. This maps to **High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**: if RBF is blocked for targeted UTXOs, victims cannot fee-bump stuck transactions, contributing to pool and network congestion.

## Likelihood Explanation
The attacker requires no special privilege, key, or majority hashpower. The entry path is the standard `send_transaction` RPC or P2P relay, accessible to any unprivileged submitter. The multi-input transaction structure used (`TX_C` spending outputs from two pool parents) is standard and valid CKB. The attacker only needs to know which UTXOs the victim intends to RBF-replace. The attack is repeatable and persistent as long as the attacker's transactions remain in the pool, and the cost is submitting three valid transactions.

## Recommendation
Collect all descendants across all conflicts into a single `HashSet` before counting, so shared descendants are deduplicated:

```rust
let mut all_descendants: HashSet<ProposalShortId> = HashSet::new();
for conflict in conflicts.iter() {
    let descendants = self.pool_map.calc_descendants(&conflict.id);
    if !descendants.is_disjoint(&ancestors) {
        return Err(Reject::RBFRejected(
            "Tx ancestors have common with conflict Tx descendants".to_string(),
        ));
    }
    // per-conflict input-in-descendants check still applies here
    let entries = descendants
        .iter()
        .filter_map(|id| self.get_pool_entry(id))
        .collect::<Vec<_>>();
    for entry in entries.iter() {
        let hash = entry.inner.transaction().hash();
        if tx_inputs.iter().any(|pt| pt.tx_hash() == hash) {
            return Err(Reject::RBFRejected(
                "new Tx contains inputs in descendants of to be replaced Tx".to_string(),
            ));
        }
    }
    all_conflicted.extend(entries);
    all_descendants.extend(descendants);
}
let replace_count = conflicts.len() + all_descendants.len();
if replace_count > MAX_REPLACEMENT_CANDIDATES {
    return Err(Reject::RBFRejected(format!(
        "Tx conflict with too many txs, conflict txs count: {}, expect <= {}",
        replace_count, MAX_REPLACEMENT_CANDIDATES,
    )));
}
```

This mirrors the deduplication already applied in `calculate_min_replace_fee` via `HashMap`. [5](#0-4) 

## Proof of Concept
**Pool state setup:**
1. Submit `TX_A`: input = on-chain cell `X`, output[0] = cell `P`
2. Submit `TX_B`: input = on-chain cell `Y`, output[0] = cell `Q`
3. Submit `TX_C_1` through `TX_C_50`: each spends one output of `TX_A` and one output of `TX_B` (fan-in, multi-input) — 50 shared descendants

**Trigger:**
- Submit `TX_NEW` via `send_transaction` RPC spending both `X` and `Y` with fee ≥ `min_replace_fee`

**Expected:** `replace_count = 2 + 50 = 52 ≤ 100`, RBF proceeds.

**Actual:** Loop iteration 1: `calc_descendants(TX_A)` = 50 → `replace_count += 51 = 51`. Loop iteration 2: `calc_descendants(TX_B)` = same 50 → `replace_count += 51 = 102 > 100`. Rejected with `"Tx conflict with too many txs, conflict txs count: 102, expect <= 100"`. [6](#0-5)

### Citations

**File:** tx-pool/src/pool.rs (L33-33)
```rust
const MAX_REPLACEMENT_CANDIDATES: usize = 100;
```

**File:** tx-pool/src/pool.rs (L104-108)
```rust
        // don't account for duplicate txs
        let replaced_fees: HashMap<_, _> = conflicts
            .iter()
            .map(|c| (c.id.clone(), c.inner.fee))
            .collect();
```

**File:** tx-pool/src/pool.rs (L613-624)
```rust
        let mut replace_count: usize = 0;
        let mut all_conflicted = conflicts.clone();
        let ancestors = self.pool_map.calc_ancestors(&short_id);
        for conflict in conflicts.iter() {
            let descendants = self.pool_map.calc_descendants(&conflict.id);
            replace_count += descendants.len() + 1;
            if replace_count > MAX_REPLACEMENT_CANDIDATES {
                return Err(Reject::RBFRejected(format!(
                    "Tx conflict with too many txs, conflict txs count: {}, expect <= {}",
                    replace_count, MAX_REPLACEMENT_CANDIDATES,
                )));
            }
```

**File:** tx-pool/src/component/links.rs (L82-84)
```rust
    pub fn calc_descendants(&self, short_id: &ProposalShortId) -> HashSet<ProposalShortId> {
        self.calc_relative_ids(short_id, Relation::Children)
    }
```

**File:** tx-pool/src/component/pool_map.rs (L294-298)
```rust
    pub(crate) fn find_conflict_tx(&self, tx: &TransactionView) -> HashSet<ProposalShortId> {
        tx.input_pts_iter()
            .filter_map(|out_point| self.edges.get_input_ref(&out_point).cloned())
            .collect()
    }
```
