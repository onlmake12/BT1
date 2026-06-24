Audit Report

## Title
`consecutive_failed` never resets on successful transaction selection, causing premature block-template truncation — (`tx-pool/src/component/tx_selector.rs`)

## Summary
In `TxSelector::txs_to_commit`, the variable `consecutive_failed` is incremented on two failure paths but is never reset to zero when a transaction is successfully packaged. The counter therefore measures **total** failures across the entire loop, not consecutive failures, violating the semantic contract implied by both the variable name and the constant `MAX_CONSECUTIVE_FAILURES`. An attacker can manufacture failing transactions to exhaust the counter and cause the loop to exit early, leaving valid fee-paying transactions out of the block template.

## Finding Description
`consecutive_failed` is initialized to `0` at line 104. [1](#0-0) 

It is incremented at line 153 when `next_cycles > cycles_limit || next_size > size_limit`: [2](#0-1) 

It is incremented again at line 184 when any ancestor of the candidate transaction is not in `Status::Proposed`: [3](#0-2) 

The success path spans lines 191–220. There is no `consecutive_failed = 0` anywhere in it: [4](#0-3) 

The constant that gates the early exit is: [5](#0-4) 

**Exploit flow:**
1. Attacker submits parent transactions (A₁…A₄₀₀₁) to the P2P network.
2. Parents are proposed in block N and move to `Status::Gap` in block N+1 (not committed).
3. Child transactions (C₁…C₄₀₀₁), each spending a respective parent, are proposed in block N+1.
4. When block N+2's template is assembled, each Cᵢ passes `sorted_proposed_iter()` (it is `Proposed`) but fails the `has_proposed` ancestor check at line 178 because Aᵢ is `Status::Gap`. [6](#0-5) 
5. Each failure increments `consecutive_failed`. After 4001 such failures, the loop breaks even if valid, high-fee transactions remain in the iterator.

## Impact Explanation
Block templates are truncated prematurely. Miners produce blocks with fewer transactions than the size/cycle limits allow, reducing fee revenue. This is a concrete, measurable performance degradation in block template generation — fitting **Low (501–2000 points): Any other important performance improvements for CKB**. The impact does not rise to High (network congestion) because the attacker must pay fees for ~8002 on-chain transactions, making sustained attack costly.

## Likelihood Explanation
An unprivileged attacker uses only the standard P2P transaction submission interface. The required pool state (parent in `Gap`, child in `Proposed`) is a normal, reachable transition: a parent proposed in block N that is not committed in block N+1 moves to `Gap`, while a child proposed in block N+1 is `Proposed`. Producing 4001 such pairs requires ~8002 transactions. This is trivially achievable on a test/local node and feasible on mainnet at moderate cost. The attack is repeatable every few blocks.

## Recommendation
Add `consecutive_failed = 0;` at the end of the success path, immediately after `self.update_modified_entries(&ancestors)` at line 220:

```rust
self.update_modified_entries(&ancestors);
consecutive_failed = 0; // reset: a successful selection breaks any failure streak
``` [7](#0-6) 

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
