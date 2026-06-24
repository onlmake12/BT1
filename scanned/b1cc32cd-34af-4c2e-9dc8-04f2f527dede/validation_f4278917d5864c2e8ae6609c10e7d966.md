Looking at the actual code to verify each claim before rendering a verdict.

All three core claims check out against the actual source:

- `consecutive_failed` is initialized at line 104, incremented at lines 153 and 184, and **never reset** anywhere in the success path (lines 207–220). [1](#0-0) 
- The `failed_txs.insert` at lines 181–182 is guarded by `if using_modified`, so pool-iterator path failures are silently dropped. [2](#0-1) 
- `sorted_proposed_iter` returns entries in descending `AncestorsScoreSortKey` order, so high-fee bad entries sort first. [3](#0-2) 
- `MAX_CONSECUTIVE_FAILURES` is 4000. [4](#0-3) 

---

Audit Report

## Title
`consecutive_failed` Never Resets and Pool-Iterator Failures Bypass `failed_txs`, Enabling Early Block-Template Loop Termination — (`tx-pool/src/component/tx_selector.rs`)

## Summary
In `TxSelector::txs_to_commit`, the `consecutive_failed` counter is incremented whenever a proposed transaction has a non-proposed ancestor but is never reset when a transaction is successfully committed. Additionally, when a transaction is sourced from the pool iterator (`using_modified = false`), the failure branch does not insert the short-id into `failed_txs`. An unprivileged attacker can submit 4001 proposed transactions whose parents remain Pending/Gap and set their fees high enough to sort above legitimate transactions, driving `consecutive_failed` past `MAX_CONSECUTIVE_FAILURES` (4000) and causing the loop to break before any valid low-fee fully-proposed transactions are evaluated.

## Finding Description
**Root cause 1 — counter never resets on success.**
`consecutive_failed` is initialized once at line 104 and incremented at lines 153 and 184, but the success path (lines 207–220, where ancestors are packaged and `fetched_txs` is updated) contains no `consecutive_failed = 0` reset. A single successful commit does not undo the accumulated count from prior failures. [1](#0-0) [5](#0-4) 

**Root cause 2 — pool-iterator failures are not recorded in `failed_txs`.**
When `using_modified = false` (entry came from `sorted_proposed_iter`), the block at lines 180–182 is skipped, so the short-id is never inserted into `failed_txs`. The guard `skip_proposed_entry` (lines 235–239) only skips entries already in `fetched_txs`, `modified_entries`, or `failed_txs`, so it provides no protection for these entries. [6](#0-5) [7](#0-6) 

**Exploit flow.**
1. Attacker submits 4001 parent–child pairs. Parents remain `Pending`/`Gap`; children reach `Status::Proposed` (normal two-phase-commit behavior once any miner includes them in a proposal zone).
2. Children are assigned high fees so `AncestorsScoreSortKey` places them above legitimate low-fee fully-proposed transactions in `sorted_proposed_iter`.
3. `txs_to_commit` dequeues each of the 4001 children first. Each hits the `!has_proposed(ancestor)` check at line 178, increments `consecutive_failed`, and `continue`s. No reset occurs on any iteration.
4. After the 4001st entry, `consecutive_failed` (4001) exceeds `MAX_CONSECUTIVE_FAILURES` (4000); the `break` at line 186 fires and the loop exits before the legitimate transactions are ever dequeued. [8](#0-7) 

## Impact Explanation
`txs_to_commit` is called by `get_block_template`. When the loop terminates early, the returned `(entries, size, cycles)` tuple contains zero (or fewer) valid committed transactions even though fully-proposed, fee-paying transactions exist in the pool. Miners produce empty or impoverished blocks, losing all fee revenue for those block intervals. Repeated across multiple block intervals this constitutes sustained liveness degradation: legitimate user transactions are indefinitely excluded from block templates, causing effective network congestion.

**Matched allowed impact: High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation
The attacker requires no privileged access, no hash power, and no miner cooperation beyond ordinary mempool relay. Submitting a transaction pair where the child is proposed while the parent stays Pending is standard CKB two-phase-commit behavior reachable via `_submit_entry` → `add_proposed`. The cost is exactly 4001 on-chain transaction fees — bounded, predictable, and repeatable. The attack can be re-executed each block interval by recycling or replacing the 4001 entries. [9](#0-8) 

## Recommendation
1. **Reset `consecutive_failed = 0` on every successful commit**, immediately after the ancestor-packaging loop at line 220, mirroring Bitcoin Core's original intent for this heuristic.
2. **Insert the short-id into `failed_txs` unconditionally** (remove the `if using_modified` guard) in both failure branches (lines 154–157 and 180–183), so `skip_proposed_entry` can prevent the same logical failure from re-entering consideration via `modified_entries`.
3. Consider replacing the absolute counter with a ratio-based or sliding-window heuristic (e.g., fail-rate over the last N candidates) that is harder to saturate with crafted inputs.

## Proof of Concept
```
1. Construct a PoolMap.
2. Insert 4001 TxEntry objects with Status::Proposed, each linked via TxLinksMap
   to a parent with Status::Gap (or Pending). Set fee = HIGH on all 4001 children.
3. Insert 100 TxEntry objects with Status::Proposed, no ancestors. Set fee = LOW.
4. Call TxSelector::new(&pool_map).txs_to_commit(usize::MAX, Cycle::MAX).
5. Assert result.0.len() == 0  // the 100 valid txs were never reached.

Expected trace:
  - Iterations 1–4001: each child hits `!has_proposed(parent)` at line 178,
    consecutive_failed increments from 0 to 4001, no reset occurs.
  - Iteration 4001: consecutive_failed (4001) > MAX_CONSECUTIVE_FAILURES (4000),
    break fires at line 185–186.
  - The 100 low-fee valid entries are never dequeued from the iterator.
``` [4](#0-3) [10](#0-9)

### Citations

**File:** tx-pool/src/component/tx_selector.rs (L50-50)
```rust
const MAX_CONSECUTIVE_FAILURES: usize = 4000;
```

**File:** tx-pool/src/component/tx_selector.rs (L104-104)
```rust
        let mut consecutive_failed = 0;
```

**File:** tx-pool/src/component/tx_selector.rs (L176-189)
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
            }
```

**File:** tx-pool/src/component/tx_selector.rs (L207-221)
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

            self.update_modified_entries(&ancestors);
        }
```

**File:** tx-pool/src/component/tx_selector.rs (L235-239)
```rust
    fn skip_proposed_entry(&self, short_id: &ProposalShortId) -> bool {
        self.fetched_txs.contains(short_id)
            || self.modified_entries.contains_key(short_id)
            || self.failed_txs.contains(short_id)
    }
```

**File:** tx-pool/src/component/pool_map.rs (L398-405)
```rust
    pub(crate) fn score_sorted_iter_by_status(
        &self,
        status: Status,
    ) -> impl Iterator<Item = &TxEntry> {
        self.entries
            .iter_by_score()
            .rev()
            .filter_map(move |entry| (entry.status == status).then_some(&entry.inner))
```

**File:** tx-pool/src/process.rs (L1016-1028)
```rust
fn _submit_entry(
    tx_pool: &mut TxPool,
    status: TxStatus,
    entry: TxEntry,
    callbacks: &Callbacks,
) -> Result<HashSet<TxEntry>, Reject> {
    let tx_hash = entry.transaction().hash();
    debug!("submit_entry {:?} {}", status, tx_hash);
    let (succ, evicts) = match status {
        TxStatus::Fresh => tx_pool.add_pending(entry.clone())?,
        TxStatus::Gap => tx_pool.add_gap(entry.clone())?,
        TxStatus::Proposed => tx_pool.add_proposed(entry.clone())?,
    };
```
