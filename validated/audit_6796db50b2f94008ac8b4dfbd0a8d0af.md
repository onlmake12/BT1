Audit Report

## Title
Unbounded Memory Allocation via Duplicate Indexes in `GetBlockTransactionsProcess::execute` — (`sync/src/relayer/get_block_transactions_process.rs`)

## Summary

`GetBlockTransactionsProcess::execute` validates that the number of indexes does not exceed `MAX_RELAY_TXS_NUM_PER_BATCH` (32767) but performs no deduplication. An unprivileged peer can send 32767 copies of index `0`, causing the node to clone and fully serialize the same transaction 32767 times into a single `BlockTransactions` response. The tentacle frame limit (4 MB for RelayV3) only rejects the send after the full allocation and serialization have already occurred, making it an ineffective guard against memory exhaustion.

## Finding Description

The guard at line 37 of `sync/src/relayer/get_block_transactions_process.rs` checks only the raw count of indexes:

```rust
if get_block_transactions.indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
```

No deduplication is performed before or after this check. The `filter_map` at lines 61–71 iterates the raw (non-deduplicated) index list and calls `.cloned()` for each occurrence, producing a `Vec<TransactionView>` that may contain 32767 copies of the same transaction:

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

These are then serialized into a `RelayMessage` at lines 80–95. The call to `message.as_bytes()` inside `async_send_message` (line 80 of `sync/src/utils.rs`) fully materializes the serialized byte buffer before it is handed to the tentacle layer:

```rust
nc.async_send_message(protocol_id, peer_index, message.as_bytes()).await
```

The tentacle frame limit for `RelayV3` is 4 MB (`network/src/protocols/support_protocols.rs`, line 130). This limit is enforced at the framing layer, which is reached only after `message.as_bytes()` has already allocated the full buffer. The existing `MAX_RELAY_TXS_BYTES_PER_BATCH` constant (1 MB, defined at `sync/src/relayer/mod.rs:61`) is applied in `prune_tx_proposal_request` for `BlockProposal` responses but is never applied in the `GetBlockTransactions` response path.

**Exploit path:**
1. Attacker connects to a target node as an unprivileged peer.
2. Attacker identifies a block stored by the target that contains a large transaction (e.g., ~500 KB witness data; valid under `MAX_BLOCK_BYTES = 597 KB` at `spec/src/consensus.rs:83`).
3. Attacker sends a `GetBlockTransactions` message with `indexes = [0u32; 32767]`.
4. The node passes the count check (32767 ≤ 32767), iterates all 32767 entries, clones the transaction 32767 times, and calls `message.as_bytes()`, allocating ~16 GB on the heap.
5. The tentacle frame rejection fires only after this allocation, providing no protection against OOM.

## Impact Explanation

This matches **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node.** A single crafted message causes the target node to attempt a ~16 GB heap allocation (32767 × ~500 KB), exhausting available RAM and triggering OOM termination or severe swap-induced stall. The node becomes completely unresponsive. No consensus deviation or economy damage is required; the impact is a reliable single-node crash.

## Likelihood Explanation

Any connected peer can send a `GetBlockTransactions` message with no authentication or privilege. The precondition — a stored block containing a large-witness transaction — is realistic on mainnet. The rate limiter at `sync/src/relayer/mod.rs:116–123` (30 req/s per `(PeerIndex, message_type)`) does not prevent a single maximally-amplified request from causing OOM; it only limits repetition rate. A single request is sufficient to exhaust memory.

## Recommendation

1. **Deduplicate indexes** before building the response, using a `HashSet` to skip repeated entries.
2. **Apply `MAX_RELAY_TXS_BYTES_PER_BATCH`** as an outbound size guard in `GetBlockTransactionsProcess::execute`, accumulating the serialized size of each transaction and stopping before the limit is exceeded, consistent with how `prune_tx_proposal_request` handles `BlockProposal` responses at `sync/src/relayer/mod.rs:582–601`.

## Proof of Concept

1. Identify a CKB block stored by the target node containing a transaction with large witness data (~500 KB).
2. Connect to the target node as an unprivileged peer on the `RelayV3` protocol.
3. Send a `GetBlockTransactions` message: `block_hash = <target block hash>`, `indexes = [0u32; 32767]`, `uncle_indexes = []`.
4. Observe the target node's RSS grow by ~16 GB as it clones and serializes 32767 copies of the transaction before `message.as_bytes()` returns.
5. The node OOMs or becomes unresponsive before the tentacle frame limit is ever checked.

A unit test can be written in `sync/src/relayer/tests/` by constructing a mock block with a large transaction, calling `GetBlockTransactionsProcess::execute` with a 32767-entry duplicate index list, and asserting that the resulting `transactions` Vec has length 1 (after the fix) rather than 32767.