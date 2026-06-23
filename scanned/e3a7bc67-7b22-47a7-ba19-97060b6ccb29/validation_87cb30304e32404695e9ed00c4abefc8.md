### Title
`consecutive_failed` Counter Never Reset in Block-Template Selector Causes Premature Loop Termination — (`File: tx-pool/src/component/tx_selector.rs`)

---

### Summary

In `TxSelector::txs_to_commit`, the `consecutive_failed` counter is incremented whenever a proposed transaction fails inclusion (due to exceeding cycles/size limits, or having non-proposed ancestors), but is **never reset to zero after a successful inclusion**. An unprivileged tx-pool submitter can flood the proposed pool with transactions whose ancestors are not proposed, accumulating 4,000 failures and triggering the `MAX_CONSECUTIVE_FAILURES` early-exit, causing the block assembler to silently skip valid, high-fee proposed transactions.

---

### Finding Description

`TxSelector::txs_to_commit` is the sole function that selects transactions for block templates. It iterates over the proposed pool sorted by fee rate and maintains a `consecutive_failed` counter:

```rust
const MAX_CONSECUTIVE_FAILURES: usize = 4000;
...
let mut consecutive_failed = 0;
...
loop {
    ...
    if next_cycles > cycles_limit || next_size > size_limit {
        consecutive_failed += 1;
        if consecutive_failed > MAX_CONSECUTIVE_FAILURES { break; }
        continue;
    }
    if ancestors_ids.iter().any(|id| !self.pool_map.has_proposed(id)) {
        consecutive_failed += 1;
        if consecutive_failed > MAX_CONSECUTIVE_FAILURES { break; }
        continue;
    }
    // SUCCESS PATH — consecutive_failed is never reset to 0 here
    ...
}
``` [1](#0-0) 

The variable is named `consecutive_failed` and the comment explicitly says *"Limit the number of attempts to add transactions to the block when it is **close to full**"*, indicating the intent is to count **consecutive** failures (as in Bitcoin Core's analogous `nConsecutiveFailed`, which is reset to 0 after each successful package). CKB's implementation omits this reset entirely. [2](#0-1) 

The second failure path — `ancestors_ids.iter().any(|id| !self.pool_map.has_proposed(id))` — is independent of block fullness. A transaction with a non-proposed ancestor fails this check regardless of how much space remains in the block. Each such failure increments `consecutive_failed` without any subsequent success resetting it. [3](#0-2) 

The `skip_proposed_entry` fast-path (for already-fetched, modified, or failed entries) does not increment the counter, so it cannot be used to dilute the attack. [4](#0-3) 

`package_txs` (called by `update_full` and `update_transactions` in the block assembler) delegates entirely to `TxSelector::txs_to_commit`, so any early exit propagates directly into the block template served to miners. [5](#0-4) [6](#0-5) 

---

### Impact Explanation

When `consecutive_failed` reaches 4,001, the loop breaks unconditionally. If the block is not yet full at that point, all remaining proposed transactions — including legitimate high-fee ones sorted lower in the iterator — are silently excluded from the block template. Miners receive a suboptimal template, losing fee revenue, and legitimate users experience delayed or indefinitely deferred transaction confirmation. The block assembler produces no error; the template is returned as if complete.

---

### Likelihood Explanation

CKB uses a two-phase commit: a transaction must first appear in a block's proposal zone (becoming "proposed") before it can be committed. An attacker who can submit transactions to the tx-pool (any unprivileged RPC caller via `send_transaction`) can:

1. Submit many child transactions with artificially high fee rates (to rank high in `sorted_proposed_iter`).
2. Arrange for them to be proposed (e.g., by being a miner, or by paying fees to get them into proposal zones) while deliberately withholding or never proposing their parent transactions.
3. These children enter the proposed pool but permanently fail the `has_proposed` ancestor check.

Each such child increments `consecutive_failed` by 1. After 4,000 such children are processed, the loop exits. A well-funded attacker or a miner acting adversarially can sustain this at the cost of 4,000 proposal-zone slots. The attack is repeatable every block template refresh cycle (triggered on every new block or tx-pool change).

---

### Recommendation

Reset `consecutive_failed` to `0` after each successful package inclusion, matching the semantics of Bitcoin Core's `nConsecutiveFailed`:

```rust
// After the successful ancestors loop:
self.update_modified_entries(&ancestors);
consecutive_failed = 0; // reset streak after success
```

Additionally, consider whether the "non-proposed ancestor" failure path should count toward `consecutive_failed` at all, since it is not a capacity signal. A separate counter or a pre-filter that excludes proposed entries with non-proposed ancestors before the main loop would eliminate the attack surface entirely.

---

### Proof of Concept

1. Attacker submits 4,001 child transactions `C_1 … C_4001`, each spending an output of a distinct parent `P_i` that is **not** submitted to the pool.
2. Attacker (or a colluding miner) includes `C_1 … C_4001` in proposal zones across consecutive blocks. Each `C_i` transitions to `Status::Proposed` in the pool.
3. On the next call to `get_block_template` → `package_txs` → `txs_to_commit`:
   - The iterator yields `C_1, C_2, …` (sorted by fee rate).
   - Each `C_i` fails `has_proposed(P_i)` → `consecutive_failed` increments.
   - At `C_4001`, `consecutive_failed > 4000` → `break`.
4. Any legitimate proposed transaction `T` sorted after `C_4001` is excluded from the template, even if the block has ample remaining cycles and bytes.
5. Miners receive a template missing `T`; `T`'s confirmation is deferred until the next template refresh, which is again vulnerable to the same attack. [7](#0-6) [8](#0-7)

### Citations

**File:** tx-pool/src/component/tx_selector.rs (L47-50)
```rust
// Limit the number of attempts to add transactions to the block when it is
// close to full; this is just a simple heuristic to finish quickly if the
// mempool has a lot of entries.
const MAX_CONSECUTIVE_FAILURES: usize = 4000;
```

**File:** tx-pool/src/component/tx_selector.rs (L97-162)
```rust
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
        loop {
            let mut using_modified = false;

            if let Some(entry) = iter.peek()
                && self.skip_proposed_entry(&entry.proposal_short_id())
            {
                iter.next();
                continue;
            }

            // First try to find a new transaction in `proposed_pool` to evaluate.
            let tx_entry: TxEntry = match (iter.peek(), self.modified_entries.next_best_entry()) {
                (Some(entry), Some(best_modified)) => {
                    if &best_modified > entry {
                        using_modified = true;
                        best_modified.clone()
                    } else {
                        // worse than `proposed_pool`
                        iter.next().cloned().expect("peek guard")
                    }
                }
                (Some(_), None) => {
                    // Either no entry in `modified_entries`
                    iter.next().cloned().expect("peek guarded")
                }
                (None, Some(best_modified)) => {
                    // We're out of entries in `proposed`; use the entry from `modified_entries`
                    using_modified = true;
                    best_modified.clone()
                }
                (None, None) => {
                    break;
                }
            };

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

**File:** tx-pool/src/component/tx_selector.rs (L174-189)
```rust
            // prepare to package tx with ancestors
            let ancestors_ids = self.pool_map.calc_ancestors(&short_id);
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

**File:** tx-pool/src/component/tx_selector.rs (L231-239)
```rust
    // Skip entries in `proposed` that are already in a block or are present
    // in `modified_entries` (which implies that the mapTx ancestor state is
    // stale due to ancestor inclusion in the block)
    // Also skip transactions that we've already failed to add.
    fn skip_proposed_entry(&self, short_id: &ProposalShortId) -> bool {
        self.fetched_txs.contains(short_id)
            || self.modified_entries.contains_key(short_id)
            || self.failed_txs.contains(short_id)
    }
```

**File:** tx-pool/src/pool.rs (L536-554)
```rust
    pub(crate) fn package_txs(
        &self,
        max_block_cycles: Cycle,
        txs_size_limit: usize,
    ) -> (Vec<TxEntry>, usize, Cycle) {
        let (entries, size, cycles) =
            TxSelector::new(&self.pool_map).txs_to_commit(txs_size_limit, max_block_cycles);

        if !entries.is_empty() {
            ckb_logger::info!(
                "[get_block_template] candidate txs count: {}, size: {}/{}, cycles:{}/{}",
                entries.len(),
                size,
                txs_size_limit,
                cycles,
                max_block_cycles
            );
        }
        (entries, size, cycles)
```

**File:** tx-pool/src/block_assembler/mod.rs (L182-214)
```rust
    pub(crate) async fn update_full(&self, tx_pool: &RwLock<TxPool>) -> Result<(), AnyError> {
        let mut current = self.current.lock().await;
        let consensus = current.snapshot.consensus();
        let max_block_bytes = consensus.max_block_bytes() as usize;

        let current_template = &current.template;
        let uncles = &current_template.uncles;

        let (proposals, txs, basic_size) = {
            let tx_pool_reader = tx_pool.read().await;
            if current.snapshot.tip_hash() != tx_pool_reader.snapshot().tip_hash() {
                return Ok(());
            }

            let proposals =
                tx_pool_reader.package_proposals(consensus.max_block_proposals_limit(), uncles);

            let basic_size = Self::basic_block_size(
                current_template.cellbase.data(),
                uncles,
                proposals.iter(),
                current_template.extension.clone(),
            );

            let txs_size_limit = max_block_bytes
                .checked_sub(basic_size)
                .ok_or(BlockAssemblerError::Overflow)?;

            let max_block_cycles = consensus.max_block_cycles();
            let (txs, _txs_size, _cycles) =
                tx_pool_reader.package_txs(max_block_cycles, txs_size_limit);
            (proposals, txs, basic_size)
        };
```
