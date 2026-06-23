### Title
Missing Deduplication of `missing_transactions` Indexes Causes Duplicate `GetBlockTransactions` Requests — (`File: sync/src/relayer/block_transactions_process.rs`)

### Summary

In the compact block relay protocol, when a node fails to reconstruct a block after receiving a `BlockTransactions` response, it merges the newly-still-missing transaction indexes with the previously-requested indexes to form a combined retry request. This merge is performed via iterator chaining without any deduplication step. Only `sort_unstable()` is called — `dedup()` is never called. As a result, any index that appears in both the new missing set and the old expected set is duplicated in the outgoing `GetBlockTransactions` message. A malicious relay peer can deliberately trigger this condition by responding with a partial `BlockTransactions` message, causing the victim node to emit a `GetBlockTransactions` with duplicate indexes, which in turn causes the responding peer to return duplicate transactions in `BlockTransactions`, corrupting the reconstruction input and stalling block propagation.

---

### Finding Description

In `sync/src/relayer/block_transactions_process.rs`, when `reconstruct_block` returns `ReconstructionResult::Missing`, the code at lines 137–149 builds the retry request:

```rust
missing_transactions = transactions          // newly-still-missing indexes
    .into_iter()
    .map(|i| i as u32)
    .chain(expected_transaction_indexes.iter().copied())  // previously requested
    .collect();
// ...
missing_transactions.sort_unstable();        // sorted, but NOT deduped
``` [1](#0-0) 

The `transactions` vector from `ReconstructionResult::Missing` contains the indexes that are **still** missing after the current reconstruction attempt. The `expected_transaction_indexes` contains the indexes that were **previously requested**. Any index that was requested before and is still missing will appear in **both** iterators, producing a duplicate entry after the chain. The subsequent `sort_unstable()` preserves duplicates; `dedup()` is never called.

The resulting `missing_transactions` vector — with duplicates — is then directly packed into the outgoing `GetBlockTransactions` message:

```rust
let content = packed::GetBlockTransactions::new_builder()
    .block_hash(block_hash.clone())
    .indexes(missing_transactions.as_slice())   // may contain duplicates
    .uncle_indexes(missing_uncles.as_slice())
    .build();
``` [2](#0-1) 

And the duplicate-containing vector is stored as the new `expected_transaction_indexes` for the next round:

```rust
let _ignore_prev_value =
    mem::replace(expected_transaction_indexes, missing_transactions);
``` [3](#0-2) 

On the receiving side, `get_block_transactions_process.rs` iterates over the indexes without any duplicate check, fetching the same transaction multiple times:

```rust
let transactions = self
    .message
    .indexes()
    .iter()
    .filter_map(|i| {
        block.transactions().get(Into::<u32>::into(i) as usize).cloned()
    })
    .collect::<Vec<_>>();
``` [4](#0-3) 

The only guard in `get_block_transactions_process.rs` is a count check against `MAX_RELAY_TXS_NUM_PER_BATCH`:

```rust
if get_block_transactions.indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
    return StatusCode::ProtocolMessageIsMalformed...
``` [5](#0-4) 

There is no check for duplicate indexes within the list.

---

### Impact Explanation

1. **Block propagation stall (primary impact):** The `BlockTransactionsVerifier::verify()` call at line 80 uses `expected_transaction_indexes` (now containing duplicates) to validate the count and ordering of received transactions. A peer responding honestly to the duplicate-index request will return duplicate transactions. The verifier's count/index alignment check will mismatch against the compact block's short-ID list, causing verification failure and preventing the block from being reconstructed. This stalls block propagation for the victim node against that peer.

2. **Bandwidth amplification:** Each retry round doubles the index list for any index that remains missing, causing the peer to transmit the same transaction data multiple times per round.

3. **Malformed-message rejection cascade:** If the duplicated index count exceeds `MAX_RELAY_TXS_NUM_PER_BATCH`, the peer returns `ProtocolMessageIsMalformed`, which may trigger a ban of the victim node by the honest peer — inverting the trust relationship. [6](#0-5) 

---

### Likelihood Explanation

The trigger requires only that a peer:
1. Relay a compact block containing at least two non-prefilled transactions.
2. Respond to the victim's `GetBlockTransactions` with a `BlockTransactions` message that omits at least one of the requested transactions.

This is fully within the capability of any unprivileged relay peer. No key material, privileged access, or majority hashpower is required. The code comment at lines 128–136 explicitly acknowledges this as a "small probability event" under normal operation, but a malicious peer can make it occur deterministically on every retry round. [7](#0-6) 

---

### Recommendation

Add `.dedup()` immediately after `.sort_unstable()` for both `missing_transactions` and `missing_uncles`:

```rust
missing_transactions.sort_unstable();
missing_transactions.dedup();          // add this

missing_uncles.sort_unstable();
missing_uncles.dedup();                // add this
```

This mirrors the pattern already used in `request_proposal_txs` where `.unique()` is called on the fresh proposals before sending:

```rust
Ok(fresh_proposals) => fresh_proposals.into_iter().unique().collect(),
``` [8](#0-7) 

---

### Proof of Concept

**Setup:** Victim node V, malicious peer P.

1. P sends V a compact block `B` containing 3 non-prefilled transactions at indexes `[1, 2, 3]`.
2. V cannot reconstruct `B` from its tx-pool; it sends `GetBlockTransactions { indexes: [1, 2, 3] }` to P. V stores `expected_transaction_indexes = [1, 2, 3]`.
3. P responds with `BlockTransactions` containing only `tx[1]` (deliberately omitting `tx[2]` and `tx[3]`).
4. V calls `reconstruct_block`; it still fails. `ReconstructionResult::Missing([2, 3], [])` is returned.
5. V builds `missing_transactions`:
   - `transactions` = `[2, 3]`
   - `chain(expected_transaction_indexes)` = `[2, 3, 1, 2, 3]`
   - After `sort_unstable()`: `[1, 2, 2, 3, 3]`
   - **No `dedup()` is called.**
6. V sends `GetBlockTransactions { indexes: [1, 2, 2, 3, 3] }` — 5 indexes for a 3-transaction block.
7. An honest peer receiving this returns `[tx1, tx2, tx2, tx3, tx3]` — 5 transactions with duplicates.
8. `BlockTransactionsVerifier::verify()` is called with `expected_transaction_indexes = [1, 2, 2, 3, 3]` and 5 received transactions. The alignment with the compact block's 3 short-IDs is broken; reconstruction fails again.
9. P can repeat step 3 indefinitely, preventing V from ever accepting block `B`. [1](#0-0)

### Citations

**File:** sync/src/relayer/block_transactions_process.rs (L80-84)
```rust
                attempt!(BlockTransactionsVerifier::verify(
                    compact_block,
                    expected_transaction_indexes,
                    &received_transactions,
                ));
```

**File:** sync/src/relayer/block_transactions_process.rs (L128-136)
```rust
                        // We need to get all transactions and uncles that do not exist locally
                        // at once to restore a block
                        //
                        // Under normal circumstances, one request is enough, when the chain occurs fork,
                        // the transaction pool may drop some transactions due to double spend check, at
                        // this time, the previously issued request to obtain transactions may not meet
                        // the needs of a one-time construction, we need to send another complete request
                        // to do so. That is, the current miss + the miss of the previous request are
                        // combined and requested once. This is a small probability event
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

**File:** sync/src/relayer/block_transactions_process.rs (L167-171)
```rust
                let content = packed::GetBlockTransactions::new_builder()
                    .block_hash(block_hash.clone())
                    .indexes(missing_transactions.as_slice())
                    .uncle_indexes(missing_uncles.as_slice())
                    .build();
```

**File:** sync/src/relayer/block_transactions_process.rs (L176-178)
```rust
                let _ignore_prev_value =
                    mem::replace(expected_transaction_indexes, missing_transactions);
                let _ignore_prev_value = mem::replace(expected_uncle_indexes, missing_uncles);
```

**File:** sync/src/relayer/get_block_transactions_process.rs (L37-43)
```rust
            if get_block_transactions.indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "Indexes count({}) > MAX_RELAY_TXS_NUM_PER_BATCH({})",
                    get_block_transactions.indexes().len(),
                    MAX_RELAY_TXS_NUM_PER_BATCH,
                ));
            }
```

**File:** sync/src/relayer/get_block_transactions_process.rs (L61-71)
```rust
            let transactions = self
                .message
                .indexes()
                .iter()
                .filter_map(|i| {
                    block
                        .transactions()
                        .get(Into::<u32>::into(i) as usize)
                        .cloned()
                })
                .collect::<Vec<_>>();
```

**File:** sync/src/relayer/mod.rs (L246-246)
```rust
                    Ok(fresh_proposals) => fresh_proposals.into_iter().unique().collect(),
```
