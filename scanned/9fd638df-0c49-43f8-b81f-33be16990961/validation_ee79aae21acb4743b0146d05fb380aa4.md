Audit Report

## Title
`GetBlocksProcess` Missing IBD State Guard Allows Peers to Force Block Serving During Initial Block Download - (File: sync/src/synchronizer/get_blocks_process.rs)

## Summary

The module-level comment in `sync/src/synchronizer/mod.rs` explicitly documents that an IBD node must respond with `packed::InIBD` to both `GetHeaders` and `GetBlocks` requests. `GetHeadersProcess::execute()` correctly enforces this invariant, but `GetBlocksProcess::execute()` contains no IBD check whatsoever, allowing any connected peer to force an IBD node to look up, serialize, and transmit full block data while it should be focused exclusively on downloading the chain.

## Finding Description

The design contract is stated at `sync/src/synchronizer/mod.rs` line 4:
> "When CKB node is in IBD mode, it will respond `packed::InIBD` to `GetHeaders` and `GetBlocks` requests"

`GetHeadersProcess::execute()` enforces this at line 53–66 of `sync/src/synchronizer/get_headers_process.rs` with an `is_initial_block_download()` guard that sends `InIBD` and returns `Status::ignored()`.

`GetBlocksProcess::execute()` at lines 33–97 of `sync/src/synchronizer/get_blocks_process.rs` has no such guard. It proceeds directly to iterate over up to `INIT_BLOCKS_IN_TRANSIT_PER_PEER` block hashes, checks `BlockStatus::BLOCK_VALID`, and spawns async tasks to send full `SendBlock` messages to the requesting peer.

The `Synchronizer::received()` dispatcher at lines 890–970 of `sync/src/synchronizer/mod.rs` has no top-level IBD guard either, unlike `Relayer::received()` which short-circuits at lines 815–818 of `sync/src/relayer/mod.rs` with an explicit IBD check before any message processing.

The exploit path is direct: connect to an IBD node → send a `packed::SyncMessage` containing `GetBlocks` with valid block hashes → the node serves full blocks without checking its own IBD state.

## Impact Explanation

The concrete impact is resource waste on the IBD node: upload bandwidth consumed serving full blocks and CPU spent on hash lookups and serialization during the most resource-intensive phase of the node lifecycle. This can slow or degrade IBD progress. This maps to **Low (501–2000 points): Any other important performance improvements for CKB**. It does not crash the node, cause consensus deviation, or produce network-wide congestion, so higher severity tiers are not justified.

## Likelihood Explanation

Any peer that establishes a connection can trigger this. No privilege, key, or special role is required. The attacker sends a standard `packed::SyncMessage` with a `GetBlocks` payload. The condition is trivially reproducible and repeatable at high frequency. There are no mitigating rate limits or connection restrictions specific to this message type during IBD.

## Recommendation

Add an IBD guard at the top of `GetBlocksProcess::execute()` in `sync/src/synchronizer/get_blocks_process.rs`, mirroring the pattern in `GetHeadersProcess::execute()`:

```rust
pub fn execute(self) -> Status {
    let active_chain = self.synchronizer.shared.active_chain();
    if active_chain.is_initial_block_download() {
        // Optionally send InIBD response here
        return Status::ignored();
    }
    // ... existing logic
}
```

Alternatively, add a top-level IBD guard in `Synchronizer::received()` before dispatching to any process handler, mirroring the pattern in `Relayer::received()` at lines 815–818 of `sync/src/relayer/mod.rs`.

## Proof of Concept

1. Start a CKB node from genesis so `is_initial_block_download()` returns `true`.
2. Connect a peer to the node.
3. From the peer, send a `packed::SyncMessage` containing a `GetBlocks` payload with the genesis block hash and any other stored valid block hashes.
4. Observe that the IBD node responds with `SendBlock` messages containing full block data instead of `InIBD` or ignoring the request.
5. Confirm by comparing `get_headers_process.rs` line 53 (IBD check present) with `get_blocks_process.rs` lines 33–97 (no IBD check anywhere in `execute()`).
6. Repeat at high frequency to measure upload bandwidth and CPU consumption on the IBD node.