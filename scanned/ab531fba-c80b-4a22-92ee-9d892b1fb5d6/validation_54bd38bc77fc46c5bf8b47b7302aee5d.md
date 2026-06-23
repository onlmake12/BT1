I have all the information needed. Let me trace the exact code path.

**Step 1: Duplicate accumulation in the `Missing` arm**

In `block_transactions_process.rs` lines 137–149, when `reconstruct_block` returns `Missing`, the new missing indexes are chained with the old `expected_transaction_indexes` and sorted — but never deduped: [1](#0-0) 

**Step 2: The stale (duplicate-containing) indexes are passed directly to `verify()`**

On the *next* `execute()` call, `verify()` is called at line 80 using the now-duplicate-containing `expected_transaction_indexes` *before* any `mem::replace`: [2](#0-1) 

**Step 3: `verify()` inflates `missing_short_ids` via duplicate indexes**

`filter_map` iterates over every entry in `indexes` including duplicates. Each duplicate index pointing to a short-id slot (not a prefilled slot) produces an additional `Some(short_id)` entry in `missing_short_ids`: [3](#0-2) 

**Step 4: The length check fails spuriously** [4](#0-3) 

**Step 5: The 4xx error code triggers a peer ban**

`BlockTransactionsLengthIsUnmatchedWithPendingCompactBlock = 410` falls in the `400..500` range, so `should_ban()` returns `Some(BAD_MESSAGE_BAN_TIME)`: [5](#0-4) 

---

### Concrete duplicate accumulation trace

Given a compact block with short-id slots at indexes `[0, 1, 2]`, initial `expected_transaction_indexes = [0, 1, 2]`:

- **Round 1**: peer sends tx for index 0 only → `reconstruct_block` returns `Missing([1, 2])` → `missing_transactions = [1,2].chain([0,1,2]) = [1,2,0,1,2]` → after `sort_unstable`: `[0,1,1,2,2]` → stored as new `expected_transaction_indexes`
- **Round 2**: `verify()` called with `[0,1,1,2,2]` → `missing_short_ids.len() = 5` but peer sends 3 transactions → `5 != 3` → `BlockTransactionsLengthIsUnmatchedWithPendingCompactBlock` → peer banned

---

### Title
Duplicate accumulation in `expected_transaction_indexes` causes spurious peer ban via `BlockTransactionsLengthIsUnmatchedWithPendingCompactBlock` — (`sync/src/relayer/block_transactions_process.rs`)

### Summary
The `Missing` reconstruction arm chains new missing indexes onto the old `expected_transaction_indexes` and sorts, but never calls `dedup()`. On the next `BlockTransactions` message, `verify()` iterates the duplicate-containing slice, inflating `missing_short_ids.len()` beyond `transactions.len()`, triggering a 4xx ban on a legitimate peer.

### Finding Description
In `BlockTransactionsProcess::execute`, the `Missing` arm at lines 137–149 builds `missing_transactions` by chaining the newly-missing indexes with the previously-stored `expected_transaction_indexes`. After `sort_unstable()` there is no `dedup()` call. The result is stored via `mem::replace` at line 177. On the subsequent `execute()` call, `BlockTransactionsVerifier::verify()` is invoked at line 80 with this duplicate-containing slice. Inside `verify()`, the `filter_map` over `block_short_ids()` emits one `Some(short_id)` per index entry — including duplicates — inflating `missing_short_ids.len()`. The length check at line 23 then fails even when the peer sends exactly the right number of transactions.

### Impact Explanation
Any legitimate peer that triggers two consecutive `Missing` reconstruction rounds (a documented, expected scenario per the comment at lines 130–136) will be banned with `BAD_MESSAGE_BAN_TIME`. This degrades compact block propagation by removing responsive peers from the node's peer set.

### Likelihood Explanation
The code comment at lines 130–136 explicitly acknowledges that multiple Missing rounds are a "small probability event" that occurs during chain forks when the tx-pool drops transactions. This is a normal operational condition, not an exotic edge case. An attacker can also deliberately trigger it by sending a compact block and then responding with partial `BlockTransactions` messages.

### Recommendation
Add `missing_transactions.dedup();` immediately after `missing_transactions.sort_unstable();` at line 148, and similarly `missing_uncles.dedup();` after line 149 in `sync/src/relayer/block_transactions_process.rs`. [6](#0-5) 

### Proof of Concept
1. Create a compact block with 3 short-id slots (indexes 0, 1, 2); store it as pending with `expected_transaction_indexes = [0, 1, 2]` for a peer.
2. Peer sends `BlockTransactions` with only the tx for index 0 → `reconstruct_block` returns `Missing([1, 2])` → after chain+sort (no dedup): `expected_transaction_indexes = [0, 1, 1, 2, 2]`.
3. Peer sends `BlockTransactions` with txs for indexes 0, 1, 2 (3 transactions).
4. `verify()` is called with `[0, 1, 1, 2, 2]` → `missing_short_ids.len() = 5`, `transactions.len() = 3` → returns `BlockTransactionsLengthIsUnmatchedWithPendingCompactBlock` → `should_ban()` returns `Some(BAD_MESSAGE_BAN_TIME)` → peer is banned.

### Citations

**File:** sync/src/relayer/block_transactions_process.rs (L80-84)
```rust
                attempt!(BlockTransactionsVerifier::verify(
                    compact_block,
                    expected_transaction_indexes,
                    &received_transactions,
                ));
```

**File:** sync/src/relayer/block_transactions_process.rs (L137-149)
```rust
                        missing_transactions = transactions
                            .into_iter()
                            .map(|i| i as u32)
                            .chain(expected_transaction_indexes.iter().copied())
                            .collect();
                        missing_uncles = uncles
                            .into_iter()
                            .map(|i| i as u32)
                            .chain(expected_uncle_indexes.iter().copied())
                            .collect();

                        missing_transactions.sort_unstable();
                        missing_uncles.sort_unstable();
```

**File:** sync/src/relayer/block_transactions_verifier.rs (L13-21)
```rust
        let missing_short_ids: Vec<packed::ProposalShortId> = indexes
            .iter()
            .filter_map(|index| {
                block_short_ids
                    .get(*index as usize)
                    .expect("should never outbound")
                    .clone()
            })
            .collect();
```

**File:** sync/src/relayer/block_transactions_verifier.rs (L23-29)
```rust
        if missing_short_ids.len() != transactions.len() {
            return StatusCode::BlockTransactionsLengthIsUnmatchedWithPendingCompactBlock
                .with_context(format!(
                    "Expected({}) != actual({})",
                    missing_short_ids.len(),
                    transactions.len(),
                ));
```

**File:** sync/src/status.rs (L165-179)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        if !(400..500).contains(&(self.code as u16)) {
            return None;
        }
        if let Some(context) = &self.context {
            // TODO: it might be worthwhile to formalize all error texts
            // that won't be banned.
            if context.contains(ARGV_TOO_LONG_TEXT) {
                return None;
            }
        }
        match self.code {
            StatusCode::GetHeadersMissCommonAncestors => Some(SYNC_USELESS_BAN_TIME),
            _ => Some(BAD_MESSAGE_BAN_TIME),
        }
```
