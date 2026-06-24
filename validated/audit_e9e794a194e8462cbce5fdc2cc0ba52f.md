The code references are accurate. Let me verify the key claims:

**Line 37 guard** — confirmed: [1](#0-0) 

**No deduplication before iteration** — confirmed: [2](#0-1) 

**`message.as_bytes()` evaluated before network send** — confirmed: [3](#0-2) 

**RelayV3 frame limit = 4 MB** — confirmed, enforced at codec layer after bytes are materialized: [4](#0-3) 

**`MAX_RELAY_TXS_BYTES_PER_BATCH` = 1 MB, used only in `prune_tx_proposal_request`** — confirmed: [5](#0-4) [6](#0-5) 

**Rate limiter = 30 req/s, single request sufficient** — confirmed: [7](#0-6) 

**Reachable by any unprivileged peer** — confirmed: [8](#0-7) 

The serialization allocation occurs at `.build()` (lines 80–95), before `async_send_message` is called, so the frame limit is an ineffective guard. All claims check out.

---

Audit Report

## Title
Unbounded Memory Allocation via Duplicate Indexes in `GetBlockTransactionsProcess::execute` — (`sync/src/relayer/get_block_transactions_process.rs`)

## Summary
`GetBlockTransactionsProcess::execute` bounds the index count to `MAX_RELAY_TXS_NUM_PER_BATCH` (32767) but performs no deduplication. An unprivileged peer can send 32767 copies of the same index, causing the node to serialize 32767 copies of the same transaction into a single `BlockTransactions` message via molecule's `.build()`, allocating gigabytes on the heap before the tentacle frame limit is ever checked.

## Finding Description
The guard at line 37 of `sync/src/relayer/get_block_transactions_process.rs` rejects only if `indexes().len() > 32767`; it does not deduplicate. The `filter_map` at lines 61–71 iterates the raw index list and calls `.cloned()` for each entry, producing a `Vec<TransactionView>` with up to 32767 copies of the same transaction. These are passed to `packed::BlockTransactions::new_builder()...build()` at lines 80–94, where molecule concatenates all transaction bytes into a single contiguous heap buffer. For a block containing a transaction with large witness data (e.g., ~100–500 KB, well within CKB's block size limit), this produces a buffer of 3–16 GB. The `message.as_bytes()` call at `sync/src/utils.rs` line 80 is evaluated as a Rust argument before being passed to `nc.async_send_message`, so the full allocation completes before the tentacle codec's 4 MB frame limit (`network/src/protocols/support_protocols.rs` line 130) is reached. `MAX_RELAY_TXS_BYTES_PER_BATCH` (1 MB, `sync/src/relayer/mod.rs` line 61) is applied only in `prune_tx_proposal_request` for `BlockProposal` responses (lines 582–601) and is never consulted in this code path. The rate limiter (30 req/s per `(PeerIndex, message_type)`, lines 116–123) does not prevent a single maximally-amplified request from exhausting memory.

## Impact Explanation
**High (10001–15000 points): Vulnerabilities which could easily crash a CKB node.** A single crafted `GetBlockTransactions` message causes the target node to attempt a multi-gigabyte heap allocation (32767 × transaction size), exhausting available RAM and triggering OOM termination or severe swap-induced stall. The node becomes completely unresponsive. No consensus deviation or economic damage is required.

## Likelihood Explanation
Any connected peer can send a `GetBlockTransactions` message with no authentication or privilege escalation. The precondition — a stored block containing a transaction with non-trivial witness data — is routinely satisfied on mainnet. A single request is sufficient; the rate limiter only limits repetition rate, not the amplification factor of a single request.

## Recommendation
1. **Deduplicate indexes** before building the response: collect `indexes()` into a `HashSet` and iterate the deduplicated set, ensuring each transaction is cloned and serialized at most once.
2. **Apply `MAX_RELAY_TXS_BYTES_PER_BATCH`** as an outbound size guard in `GetBlockTransactionsProcess::execute`: accumulate the serialized size of each transaction via `tx.data().total_size()` and stop adding transactions once the limit is reached, consistent with the pattern in `prune_tx_proposal_request` at `sync/src/relayer/mod.rs` lines 582–601.

## Proof of Concept
1. Identify a CKB block stored by the target node containing a transaction with large witness data (e.g., ~100 KB or more).
2. Connect to the target node as an unprivileged peer on the `RelayV3` protocol.
3. Send a `GetBlockTransactions` message: `block_hash = <target block hash>`, `indexes = [0u32; 32767]`, `uncle_indexes = []`.
4. Observe the target node's RSS grow by N × 32767 bytes (where N is the transaction size) as molecule's `.build()` concatenates 32767 copies of the transaction into a single buffer.
5. The node OOMs or becomes unresponsive before the tentacle frame limit is checked.

A unit test can be written in `sync/src/relayer/tests/` by constructing a mock block with a large transaction, calling `GetBlockTransactionsProcess::execute` with a 32767-entry duplicate index list, and asserting that the resulting serialized message size does not exceed `MAX_RELAY_TXS_BYTES_PER_BATCH` (after the fix) rather than growing proportionally to 32767 duplicates.

### Citations

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

**File:** sync/src/utils.rs (L79-81)
```rust
    if let Err(err) = nc
        .async_send_message(protocol_id, peer_index, message.as_bytes())
        .await
```

**File:** network/src/protocols/support_protocols.rs (L130-130)
```rust
            SupportProtocols::RelayV3 => 4 * 1024 * 1024,     // 4   MB
```

**File:** sync/src/relayer/mod.rs (L61-61)
```rust
pub const MAX_RELAY_TXS_BYTES_PER_BATCH: usize = 1024 * 1024;
```

**File:** sync/src/relayer/mod.rs (L116-123)
```rust
        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```

**File:** sync/src/relayer/mod.rs (L151-155)
```rust
            packed::RelayMessageUnionReader::GetBlockTransactions(reader) => {
                GetBlockTransactionsProcess::new(reader, self, nc, peer)
                    .execute()
                    .await
            }
```

**File:** sync/src/relayer/mod.rs (L582-601)
```rust
        let mut relay_bytes = 0;
        let mut relay_proposals = Vec::new();
        for (peer_index, txs) in peer_txs {
            for tx in txs {
                let data = tx.data();
                let tx_size = data.total_size();
                if relay_bytes + tx_size > MAX_RELAY_TXS_BYTES_PER_BATCH {
                    send_block_proposals(nc, peer_index, std::mem::take(&mut relay_proposals))
                        .await;
                    relay_bytes = tx_size;
                } else {
                    relay_bytes += tx_size;
                }
                relay_proposals.push(data);
            }
            if !relay_proposals.is_empty() {
                send_block_proposals(nc, peer_index, std::mem::take(&mut relay_proposals)).await;
                relay_bytes = 0;
            }
        }
```
