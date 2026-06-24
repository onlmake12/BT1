Audit Report

## Title
Missing IBD State Check in `GetBlocksProcess` Allows Block Serving During Initial Block Download - (File: `sync/src/synchronizer/get_blocks_process.rs`)

## Summary

`GetBlocksProcess::execute()` omits the IBD guard that the module's own documentation requires and that `GetHeadersProcess::execute()` correctly implements. Any unprivileged peer can send `GetBlocks` messages to a node in IBD and receive full block data instead of the documented `InIBD` response, causing unnecessary disk reads and outbound bandwidth consumption that competes with the node's own IBD download traffic.

## Finding Description

The module-level comment in `sync/src/synchronizer/mod.rs` lines 1–7 explicitly documents the contract:

> "When CKB node is in IBD mode, it will respond `packed::InIBD` to `GetHeaders` and `GetBlocks` requests"

`GetHeadersProcess::execute()` at `sync/src/synchronizer/get_headers_process.rs` lines 53–66 correctly implements this: it calls `active_chain.is_initial_block_download()`, invokes `self.send_in_ibd()`, and returns `Status::ignored()`.

`GetBlocksProcess::execute()` at `sync/src/synchronizer/get_blocks_process.rs` lines 33–97 has no such check. It proceeds immediately to iterate over requested block hashes, check `BLOCK_VALID` status via `active_chain.contains_block_status`, retrieve full blocks via `active_chain.get_block`, and spawn async tasks to send each block back to the requesting peer.

Both handlers are dispatched from the same `try_process` match arm in `sync/src/synchronizer/mod.rs` lines 396–422 with no top-level IBD guard on the `Synchronizer` side (unlike `Relayer`, which applies a blanket IBD guard at `sync/src/relayer/mod.rs` lines 815–818). The IBD state is computed in `shared/src/shared.rs` lines 382–394 and is stable once the node exits IBD.

The exploit path is: connect as any peer → send `GetBlocks` with valid block hashes → node performs RocksDB lookups and spawns async send tasks returning full `SendBlock` frames, instead of the specified `InIBD` response.

## Impact Explanation

The concrete impact is unnecessary disk I/O (RocksDB block lookups) and outbound bandwidth consumption on a node during IBD, competing with the node's own IBD download traffic and potentially degrading IBD completion speed. This matches **Low (501–2000 points): Any other important performance improvements for CKB**. The documented protocol contract is also violated, causing peers that correctly implement the protocol to receive unexpected `SendBlock` frames instead of `InIBD` signals.

## Likelihood Explanation

The entry path requires only a standard P2P connection. No special privileges, keys, or hash power are needed. Nodes remain in IBD for an extended period after first startup (until the tip timestamp is within `MAX_TIP_AGE` of wall clock), making the exposure window long and predictable. The contrast with `GetHeadersProcess` is directly observable: sending `GetHeaders` to the same IBD node returns `InIBD`, while `GetBlocks` returns full block data.

## Recommendation

Add an IBD guard at the top of `GetBlocksProcess::execute()`, mirroring the pattern in `GetHeadersProcess::execute()` (`sync/src/synchronizer/get_headers_process.rs` lines 53–66). Add a `send_in_ibd` helper to `GetBlocksProcess` analogous to the one at lines 101–115 of `get_headers_process.rs`. The guard should call `active_chain.is_initial_block_download()`, send `InIBD`, and return `Status::ignored()` before any block hash iteration begins.

## Proof of Concept

1. Start a fresh CKB node and confirm IBD state via `get_blockchain_info` → `is_initial_block_download: true`.
2. Connect a custom peer using the Sync protocol.
3. Send a `GetBlocks` sync message containing any known early block hash (e.g., block at height 1).
4. Observe the node responds with a `SendBlock` message containing the full block, not an `InIBD` message.
5. For contrast, send a `GetHeaders` message to the same node; observe it returns `InIBD` and logs "Ignoring getheaders from peer=… because the node is in initial block download stage."
6. Send `GetBlocks` at high frequency with up to `INIT_BLOCKS_IN_TRANSIT_PER_PEER` hashes per message; observe elevated disk I/O and outbound bandwidth on the IBD node.