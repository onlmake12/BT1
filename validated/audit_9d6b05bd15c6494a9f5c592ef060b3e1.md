### Title
Missing `dedup()` After `sort_unstable()` Allows Unbounded Growth of `expected_transaction_indexes` Vec via Repeated Wrong `BlockTransactions` — (`sync/src/relayer/block_transactions_process.rs`)

---

### Summary

An unprivileged remote peer can repeatedly send `BlockTransactions` messages containing transactions that do not match the compact block's short IDs. Each round-trip causes `missing_transactions` to be rebuilt by chaining new missing indexes onto the previous `expected_transaction_indexes` without deduplication, growing the stored Vec by up to K entries per message. Over time this produces unbounded memory growth in `pending_compact_blocks` and a corresponding stream of outbound `GetBlockTransactions` messages.

---

### Finding Description

In `BlockTransactionsProcess::execute`, when `reconstruct_block` returns `ReconstructionResult::Missing`, the new missing indexes are chained with the old `expected_transaction_indexes`: [1](#0-0) 

`sort_unstable()` is called but **`dedup()` is never called**. The sorted-but-not-deduplicated Vec is then stored back as the new `expected_transaction_indexes`: [2](#0-1) 

The `missing_indexes` returned by `reconstruct_block` are positions in `block_transactions` that are `None` — bounded by K (the compact block's short_id count): [3](#0-2) 

Growth per round:
- Round 0: `expected` = K indexes
- Round 1: new_missing (≤K) + old_expected (K) → stored as 2K entries (duplicates kept)
- Round N: stored as (N+1)·K entries

The attacker is subject to the rate limiter (30 req/sec per peer): [4](#0-3) 

But `BlockTransactions` is not exempt from rate limiting (only `CompactBlock` is exempt): [5](#0-4) 

At 30 req/sec with K=500 short_ids, the Vec grows at ~15,000 `u32` entries/sec (~60 KB/sec). Over one hour: ~216 million entries (~864 MB) per pending block per peer. The attacker is not banned — `CompactBlockRequiresFreshTransactions` is a non-ban status code returned at line 183, and sending wrong-content transactions that fail short_id matching is treated as a normal "missing" scenario, not a protocol violation. [6](#0-5) 

---

### Impact Explanation

- **Memory exhaustion**: `pending_compact_blocks` inner state grows without bound for each (block_hash, peer) pair under attack.
- **Network amplification**: Each incoming `BlockTransactions` triggers an outbound `GetBlockTransactions` containing the ever-growing (and duplicate-filled) index list, amplifying traffic.
- **Scope match**: Causes CKB network congestion with low cost — attacker only needs a single P2P connection and a valid compact block in flight.

---

### Likelihood Explanation

The attacker needs only:
1. A P2P connection to the victim node.
2. A compact block currently pending for their peer (normal during block propagation, or the attacker can relay a real compact block to trigger the state).
3. Repeated `BlockTransactions` messages with transactions whose short IDs do not match the compact block's short IDs.

No PoW, no keys, no privileged access required. The rate limiter slows but does not stop the attack.

---

### Recommendation

Add `.dedup()` immediately after `.sort_unstable()` for both `missing_transactions` and `missing_uncles`:

```rust
missing_transactions.sort_unstable();
missing_transactions.dedup();          // ← add this
missing_uncles.sort_unstable();
missing_uncles.dedup();                // ← add this
```

Additionally, consider capping the Vec length to the compact block's short_id count and banning peers that repeatedly send non-matching transactions.

---

### Proof of Concept

1. Connect to a victim node as a peer.
2. Relay a valid compact block with K=100 short_ids; the node sends `GetBlockTransactions([0..99])`.
3. Respond with `BlockTransactions` containing 100 transactions whose `proposal_short_id()` values do not match any of the compact block's short IDs.
4. The node calls `reconstruct_block`, gets `Missing([0..99])`, chains it with `expected=[0..99]`, stores 200 entries (with duplicates), and sends `GetBlockTransactions([0,0,1,1,...,99,99])`.
5. Repeat step 3. After N iterations, `expected_transaction_indexes` has (N+1)·100 entries.
6. Assert `expected_transaction_indexes.len() == (N+1) * 100` — confirming unbounded growth.

### Citations

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

**File:** sync/src/relayer/block_transactions_process.rs (L176-178)
```rust
                let _ignore_prev_value =
                    mem::replace(expected_transaction_indexes, missing_transactions);
                let _ignore_prev_value = mem::replace(expected_uncle_indexes, missing_uncles);
```

**File:** sync/src/relayer/block_transactions_process.rs (L180-185)
```rust
                if collision {
                    return StatusCode::CompactBlockMeetsShortIdsCollision.with_context(block_hash);
                } else {
                    return StatusCode::CompactBlockRequiresFreshTransactions
                        .with_context(block_hash);
                }
```

**File:** sync/src/relayer/mod.rs (L89-92)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** sync/src/relayer/mod.rs (L113-123)
```rust
        let should_check_rate =
            !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));

        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```

**File:** sync/src/relayer/mod.rs (L531-545)
```rust
            let missing_indexes: Vec<usize> = block_transactions
                .iter()
                .enumerate()
                .filter_map(|(i, t)| if t.is_none() { Some(i) } else { None })
                .collect();

            debug_target!(
                crate::LOG_TARGET_RELAY,
                "block reconstruction failed, block hash: {}, missing: {}, total: {}",
                compact_block.calc_header_hash(),
                missing_indexes.len(),
                compact_block.short_ids().len(),
            );

            ReconstructionResult::Missing(missing_indexes, missing_uncles)
```
