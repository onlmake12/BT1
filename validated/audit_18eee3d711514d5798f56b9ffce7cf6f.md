The code confirms the claim. Let me verify the key facts:

1. `mod.rs` line 4 explicitly documents: "When CKB node is in IBD mode, it will respond `packed::InIBD` to `GetHeaders` and `GetBlocks` requests"
2. `get_headers_process.rs` lines 53–66: IBD guard is present and correct
3. `get_blocks_process.rs` lines 33–97: No IBD check exists — proceeds directly to `active_chain.contains_block_status()` and `active_chain.get_block()` for any peer
4. `mod.rs` lines 407–411: No pre-dispatch IBD gate for `GetBlocks`

The bug is real and confirmed. The impact is performance degradation during IBD for a single node (forced RocksDB reads + block serving), not a crash or network-wide congestion. This maps to the "Low" bounty tier.

---

Audit Report

## Title
Missing IBD State Guard in `GetBlocksProcess::execute()` Allows Block Serving During Initial Block Download — (`File: sync/src/synchronizer/get_blocks_process.rs`)

## Summary
The module-level documentation for `sync/src/synchronizer/mod.rs` explicitly states that both `GetHeaders` and `GetBlocks` messages must be rejected with an `InIBD` response during IBD mode. `GetHeadersProcess::execute()` correctly enforces this guard, but `GetBlocksProcess::execute()` has no equivalent check, allowing any inbound peer to force a node in IBD to perform RocksDB block lookups and serve full blocks unconditionally. This violates the documented IBD isolation invariant and wastes resources during the most resource-intensive phase of node operation.

## Finding Description
The module documentation at `sync/src/synchronizer/mod.rs` line 4 is unambiguous: both `GetHeaders` and `GetBlocks` must respond with `packed::InIBD` during IBD. `GetHeadersProcess::execute()` at `get_headers_process.rs` lines 53–66 correctly calls `active_chain.is_initial_block_download()`, sends `InIBD`, and returns `Status::ignored()`. `GetBlocksProcess::execute()` at `get_blocks_process.rs` lines 33–97 has no such check. After the `MAX_HEADERS_LEN` length guard (line 36), it immediately calls `active_chain.active_chain()` (line 43), iterates up to `INIT_BLOCKS_IN_TRANSIT_PER_PEER` hashes, performs `contains_block_status()` (line 60) and `get_block()` (line 68) for each, and spawns async tasks to send `SendBlock` responses (lines 78–83). The dispatch in `mod.rs` lines 407–411 routes `GetBlocks` directly to `GetBlocksProcess` with no pre-dispatch IBD gate. Any peer that completes the CKB handshake can trigger this path.

## Impact Explanation
An attacker-controlled inbound peer can send `GetBlocks` messages with up to `MAX_HEADERS_LEN` (2000) block hashes while the target is in IBD. For each hash the node performs a `BlockStatus` RocksDB lookup and, for valid hashes, a full block retrieval and async `SendBlock` dispatch — all competing with the node's own IBD download and verification pipeline. This degrades IBD throughput. The impact is localized to a single node's IBD performance and maps to **Low (501–2000 points): Any other important performance improvements for CKB**.

## Likelihood Explanation
`GetBlocks` is a standard, unauthenticated P2P sync message. Any peer completing the CKB handshake can send it. No special privilege, key, or majority hashpower is required. CKB nodes accept inbound connections by default, so the attacker only needs a TCP connection to the P2P port and a well-formed `GetBlocks` message while the target is in IBD. The condition is trivially repeatable.

## Recommendation
Add an IBD guard at the top of `GetBlocksProcess::execute()` in `sync/src/synchronizer/get_blocks_process.rs`, mirroring `GetHeadersProcess`:

```rust
pub fn execute(self) -> Status {
    let active_chain = self.synchronizer.shared.active_chain();
    if active_chain.is_initial_block_download() {
        // Optionally send InIBD response
        return Status::ignored();
    }
    // ... existing logic
}
```

This aligns the implementation with the documented invariant at `mod.rs` line 4 and with the behavior of `GetHeadersProcess`.

## Proof of Concept
1. Start a CKB node from genesis (tip timestamp far behind wall clock → `is_initial_block_download()` returns `true`).
2. Connect as an inbound peer via the P2P port and complete the handshake.
3. Send a `SyncMessage::GetBlocks` containing valid block hashes (e.g., genesis hash).
4. Observe: the node performs database lookups and sends `SendBlock` responses without sending `InIBD`.
5. Contrast: send `SyncMessage::GetHeaders` to the same node in IBD — the node correctly responds with `InIBD` and ignores the request.
6. The asymmetry between `GetHeaders` (guarded, `get_headers_process.rs` line 53) and `GetBlocks` (unguarded, `get_blocks_process.rs` line 33) during IBD is the root cause.