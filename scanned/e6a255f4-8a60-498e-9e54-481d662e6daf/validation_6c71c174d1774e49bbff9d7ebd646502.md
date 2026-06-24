Audit Report

## Title
Full Frozen-Block Deserialization on Partial `GetBlockTransactions` Request — (`sync/src/relayer/get_block_transactions_process.rs`)

## Summary
`GetBlockTransactionsProcess::execute` calls `shared.store().get_block(&block_hash)` without checking whether the block is frozen. For frozen blocks, `get_block()` reads the entire serialized block from the flat-file freezer and calls `.to_entity().into_view()`, fully deserializing every transaction, uncle, proposal, and extension field into heap-allocated Rust objects — even when only a single transaction index is requested. The per-peer rate limiter (30 req/s, 128 max peers) permits up to 3,840 such full-block deserializations per second, causing unnecessary CPU and memory overhead proportional to full block size rather than the size of the requested transactions.

## Finding Description
In `sync/src/relayer/get_block_transactions_process.rs` at line 60, the handler unconditionally calls `shared.store().get_block(&block_hash)`. In `store/src/store.rs` lines 44–53, `get_block()` detects a frozen block (`header.number() < freezer.number()`), calls `freezer.retrieve(header.number())` to read the entire serialized block from disk, then calls `raw_block_reader.to_entity().into_view()` which fully materializes every field of the block into heap-allocated Rust objects. Only after this full materialization does the handler at lines 61–71 extract the peer-specified transaction indexes and discard the rest.

The freezer's `retrieve` implementation in `freezer/src/freezer_files.rs` lines 187–190 reads exactly `end_offset - start_offset` bytes (the full block) from disk — there is no mechanism to read a sub-range for a specific transaction. This means disk I/O is proportional to the full block size regardless of which store path is used. However, the `get_transaction_with_info` path at `store/src/store.rs` lines 321–335 demonstrates the correct CPU pattern: it reads the full block from disk but calls `.to_entity().into_view()` only on the specific `tx_reader` at the requested index, avoiding full block deserialization. The `get_block()` path used by this handler has no such optimization.

The rate limiter in `sync/src/relayer/mod.rs` lines 91–92 allows 30 requests per second per `(PeerIndex, message_type)` key. With `MAX_RELAY_PEERS = 128` (line 59), this permits up to 3,840 full frozen-block deserializations per second across all peers. Each deserialization allocates and populates heap objects for every transaction, uncle, proposal, and extension in the block, regardless of how many transactions were actually requested.

Existing guards (index count check against `MAX_RELAY_TXS_NUM_PER_BATCH` at lines 37–43) only bound the number of requested indexes, not the cost of serving them from frozen storage.

## Impact Explanation
This is a **suboptimal implementation of the CKB state storage mechanism** (Medium, 2001–10000 points). An unprivileged peer can induce unnecessary CPU deserialization and memory allocation proportional to the full frozen block size, multiplied by the rate limit across all peers. This degrades node responsiveness for legitimate sync and relay operations. The impact does not rise to "easily crash a CKB node" because the rate limiter bounds the maximum throughput and the disk I/O cost is inherent to the freezer's block-granular storage format; however, the unnecessary full-block molecule deserialization at scale represents a concrete, exploitable inefficiency in the storage access pattern.

## Likelihood Explanation
The attack requires only a standard P2P connection and knowledge of any frozen block hash, which is trivially obtained from any block explorer or by syncing the chain. No privileged access, leaked keys, or special protocol knowledge is required. The attacker can sustain the attack indefinitely at 30 req/s per connection, and can open up to 128 connections to multiply the effect. The rate limiter provides partial mitigation but does not prevent the CPU amplification at scale.

## Recommendation
In `GetBlockTransactionsProcess::execute`, replace the `get_block()` call for frozen blocks with a path that avoids full block deserialization. The correct pattern is already present in `get_transaction_with_info` (`store/src/store.rs` lines 321–335): use `packed::BlockReader::from_compatible_slice` to access the raw block bytes, then call `.to_entity().into_view()` only on the specific `tx_reader` entries at the requested indexes, rather than on the full block. Alternatively, add a dedicated `get_block_transactions_from_freezer` store method that accepts a slice of transaction indexes and returns only those deserialized transactions, mirroring the partial-access pattern already used for individual transaction lookups.

## Proof of Concept
1. Run a CKB node past the freeze threshold so that at least one block is frozen (i.e., `block.number() < freezer.number()`).
2. Obtain the hash of a frozen block with many transactions from any block explorer or by querying the node's RPC.
3. Connect as a peer and send a `GetBlockTransactions` relay message with `block_hash = <frozen_block_hash>` and `indexes = [0]` (one transaction).
4. Instrument `freezer_files.rs` `retrieve` to log bytes read, or observe the `ckb_freezer_read` metric.
5. Observe that bytes read equals the full serialized block size, and that full molecule deserialization occurs for every field in the block.
6. Repeat at 30 req/s from multiple peer connections; observe CPU usage and `ckb_freezer_read` metrics growing proportionally to `num_peers × block_size × 30`, while the actual data served to the peer is only the bytes for the single requested transaction.