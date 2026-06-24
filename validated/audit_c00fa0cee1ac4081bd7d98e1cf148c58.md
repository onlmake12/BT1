The code is confirmed. Let me verify the exact lines referenced in the claim.

Audit Report

## Title
`consecutive_failed` never resets on successful transaction selection, causing premature block-template truncation — (File: tx-pool/src/component/tx_selector.rs)

## Summary
In `TxSelector::txs_to_commit`, the variable `consecutive_failed` is incremented on two failure branches but is never reset to zero when a transaction package is successfully selected. The counter therefore accumulates total failures across the entire loop rather than tracking a streak of consecutive failures, violating the semantic contract implied by the variable name and `MAX_CONSECUTIVE_FAILURES`. An attacker can manufacture a pool state with many `Proposed` child transactions whose parents are in `Status::Gap`, exhausting the counter and causing the loop to exit before valid fee-paying transactions are evaluated.

## Finding Description
`consecutive_failed` is initialized to `0` at line 104. [1](#0-0) 

It is incremented at line 153 when the candidate package exceeds `cycles_limit` or `size_limit`: [2](#0-1) 

It is incremented again at line 184 when any ancestor of the candidate is not in `Status::Proposed`: [3](#0-2) 

The success path (lines 191–220) contains no `consecutive_failed = 0` assignment anywhere: [4](#0-3) 

The early-exit threshold is `MAX_CONSECUTIVE_FAILURES = 4000`: [5](#0-4) 

`has_proposed` returns `true` only for `Status::Proposed` entries, so any ancestor in `Status::Gap` causes the ancestor check to fail: [6](#0-5) 

`Status::Gap` is a distinct, reachable status — a transaction proposed in block N that is not committed moves to `Gap` in block N+1: [7](#0-6) 

**Exploit flow:**
1. Attacker submits 4001 parent transactions (A₁…A₄₀₀₁) with low fees to the P2P network.
2. Parents are included in block N's proposal set and move to `Status::Proposed`; they are not committed in block N, so they transition to `Status::Gap` in block N+1.
3. Attacker submits 4001 child transactions (C₁…C₄₀₀₁), each spending a respective parent, in time for block N+1's proposal set; children enter `Status::Proposed`.
4. When block N+2's template is assembled, `sorted_proposed_iter()` yields each Cᵢ (it is `Proposed`). The ancestor check at line 176–178 finds Aᵢ in `Status::Gap`, so `has_proposed(Aᵢ)` returns `false`, and `consecutive_failed` is incremented.
5. After 4001 such increments, `consecutive_failed > MAX_CONSECUTIVE_FAILURES` is true and the loop breaks, even if valid high-fee transactions remain in the iterator.

## Impact Explanation
Block templates are truncated prematurely. Miners produce blocks with fewer transactions than the size/cycle limits allow, reducing fee revenue and block space utilization. This is a concrete, measurable performance degradation in block template generation, fitting **Low (501–2000 points): Any other important performance improvements for CKB**. The impact does not rise to High because the attacker must pay fees for ~8002 on-chain transactions, making sustained attack costly, and the effect is limited to block template quality rather than node stability or consensus.

## Likelihood Explanation
An unprivileged attacker uses only the standard P2P transaction submission interface. The required pool state — parent in `Gap`, child in `Proposed` — is a normal, reachable transition requiring no special privileges or victim mistakes. Producing 4001 such pairs requires ~8002 transactions, which is feasible on mainnet at moderate cost. The attack is repeatable every few blocks as long as the attacker continues to fund it.

## Recommendation
Add `consecutive_failed = 0;` at the end of the success path, immediately after `self.update_modified_entries(&ancestors)` at line 220:

```rust
self.update_modified_entries(&ancestors);
consecutive_failed = 0; // reset: a successful selection breaks any failure streak
```

This restores the intended semantics: the counter tracks only a streak of consecutive failures, not total failures across the loop. [8](#0-7) 

## Proof of Concept
1. Construct a `PoolMap` with 4001 transactions Cᵢ each having one ancestor Aᵢ in `Status::Gap` and Cᵢ itself in `Status::Proposed`. Insert one additional valid transaction V (all ancestors `Proposed`, fits within limits) at a sort position after all Cᵢ.
2. Call `TxSelector::new(&pool_map).txs_to_commit(size_limit, cycles_limit)`.
3. Assert: V is **absent** from the returned `entries` vector despite fitting within size and cycle limits — `consecutive_failed` reached 4001 before V was evaluated.
4. Apply the one-line fix (`consecutive_failed = 0` after `update_modified_entries`).
5. Re-run; assert V is now **present** in `entries`.

### Citations

**File:** tx-pool/src/component/tx_selector.rs (L50-50)
```rust
const MAX_CONSECUTIVE_FAILURES: usize = 4000;
```

**File:** tx-pool/src/component/tx_selector.rs (L104-104)
```rust
        let mut consecutive_failed = 0;
```

**File:** tx-pool/src/component/tx_selector.rs (L152-161)
```rust
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
```

**File:** tx-pool/src/component/tx_selector.rs (L176-188)
```rust
            if ancestors_ids
                .iter()
                .any(|id| !self.pool_map.has_proposed(id))
            {
                if using_modified {
                    self.modified_entries.remove(&short_id);
                    self.failed_txs.insert(short_id.clone());
                }
                consecutive_failed += 1;
                if consecutive_failed > MAX_CONSECUTIVE_FAILURES {
                    break;
                }
                continue;
```

**File:** tx-pool/src/component/tx_selector.rs (L191-221)
```rust
            let mut ancestors = ancestors_ids
                .iter()
                .filter_map(only_unconfirmed)
                .cloned()
                .collect::<Vec<TxEntry>>();

            // sort ancestors by ancestors_count,
            // if A is an ancestor of B, B.ancestors_count must large than A
            ancestors.sort_unstable_by_key(|entry| entry.ancestors_count);
            ancestors.push(tx_entry.to_owned());

            let ancestors: LinkedHashMap<ProposalShortId, TxEntry> = ancestors
                .into_iter()
                .map(|entry| (entry.proposal_short_id(), entry))
                .collect();

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

            self.update_modified_entries(&ancestors);
        }
```

**File:** tx-pool/src/component/pool_map.rs (L23-28)
```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Status {
    Pending,
    Gap,
    Proposed,
}
```

**File:** tx-pool/src/component/pool_map.rs (L169-171)
```rust
    pub(crate) fn has_proposed(&self, id: &ProposalShortId) -> bool {
        self.get_proposed(id).is_some()
    }
```
