The code confirms all claims in the report. Key verifications:

1. `sync/src/synchronizer/mod.rs` line 4 explicitly documents the contract: [1](#0-0) 

2. `GetHeadersProcess::execute()` has the IBD guard at lines 53–65: [2](#0-1) 

3. `GetBlocksProcess::execute()` has **no** IBD check — it proceeds directly to block hash processing: [3](#0-2) 

4. Both are dispatched from the same `try_process` match arm: [4](#0-3) 

---

Audit Report

## Title
Missing IBD State Guard in `GetBlocksProcess::execute()` Allows Peers to Bypass IBD Isolation - (File: `sync/src/synchronizer/get_blocks_process.rs`)

## Summary
The module-level documentation for the synchronizer explicitly contracts that a CKB node in IBD mode must respond with `InIBD` to both `GetHeaders` and `GetBlocks` requests. `GetHeadersProcess::execute()` correctly implements this guard, but `GetBlocksProcess::execute()` has no equivalent check. Any inbound peer can send `GetBlocks` messages to an IBD node and receive full serialized block bodies in response, wasting CPU and bandwidth during the critical sync phase.

## Finding Description
`sync/src/synchronizer/mod.rs` line 4 documents the contract: "When CKB node is in IBD mode, it will respond `packed::InIBD` to `GetHeaders` and `GetBlocks` requests."

`GetHeadersProcess::execute()` (`sync/src/synchronizer/get_headers_process.rs`, lines 53–65) correctly calls `active_chain.is_initial_block_download()`, sends `InIBD`, and returns `Status::ignored()`.

`GetBlocksProcess::execute()` (`sync/src/synchronizer/get_blocks_process.rs`, lines 33–97) has no such check. It proceeds directly to:
1. Accepting up to `MAX_HEADERS_LEN` (2000) block hashes from the peer.
2. Iterating up to `INIT_BLOCKS_IN_TRANSIT_PER_PEER` (32) of them.
3. Checking `BlockStatus::BLOCK_VALID` (line 60) — blocks already verified during IBD pass this check.
4. Looking up each block via `active_chain.get_block()`, serializing it, and spawning an async send task per block.

Both handlers are dispatched from the same `try_process` match arm (`sync/src/synchronizer/mod.rs`, lines 407–411). The `BLOCK_VALID` guard does not protect against this: during IBD, already-downloaded and verified blocks satisfy it, so the attacker can use any public on-chain block hashes from the portion of the chain already synced.

## Impact Explanation
**Low (501–2000 points): Any other important performance improvements for CKB.** The missing guard causes the IBD node to perform unnecessary block lookups, serializations, and async task spawns in response to arbitrary inbound peers, wasting CPU and bandwidth during the most resource-intensive phase of node operation. The documented IBD isolation policy is violated. Impact is bounded to a single node's performance degradation and does not rise to node crash, consensus deviation, or network-wide congestion.

## Likelihood Explanation
Any peer that can establish a P2P connection to an IBD node can trigger this. CKB nodes accept inbound connections during IBD. No special privileges, keys, or hashpower are required. Valid block hashes are public on-chain data. The attacker can repeat the request continuously at low cost, as each `GetBlocks` message is cheap to construct and send.

## Recommendation
Add an IBD guard at the start of `GetBlocksProcess::execute()`, mirroring `GetHeadersProcess`:

```rust
pub fn execute(self) -> Status {
    let active_chain = self.synchronizer.shared.active_chain();

    if active_chain.is_initial_block_download() {
        return Status::ignored();
    }

    let block_hashes = self.message.block_hashes();
    // ... rest of existing logic
}
```

This matches the documented contract in `sync/src/synchronizer/mod.rs` and the existing pattern in `sync/src/synchronizer/get_headers_process.rs`.

## Proof of Concept
1. Start a CKB node with a tip timestamp more than `MAX_TIP_AGE` (24 hours) behind wall clock — it enters IBD mode.
2. Connect an attacker peer via the Sync protocol as an inbound peer.
3. Collect any 32 valid block hashes from the public chain (blocks already verified by the IBD node satisfy `BLOCK_VALID`).
4. Send a `SyncMessage::GetBlocks` containing those 32 hashes.
5. Observe that the node responds with `SendBlock` messages for each hash rather than `InIBD`.
6. Send the same `GetHeaders` message with a valid locator — observe the node responds with `InIBD` and ignores the request.
7. Repeat step 4 in a tight loop; observe CPU and network I/O increase on the IBD node with no rate limiting or IBD guard stopping the requests.

### Citations

**File:** sync/src/synchronizer/mod.rs (L1-7)
```rust
//! CKB node has initial block download phase (IBD mode) like Bitcoin:
//! <https://btcinformation.org/en/glossary/initial-block-download>
//!
//! When CKB node is in IBD mode, it will respond `packed::InIBD` to `GetHeaders` and `GetBlocks` requests
//!
//! And CKB has a headers-first synchronization style like Bitcoin:
//! <https://btcinformation.org/en/glossary/headers-first-sync>
```

**File:** sync/src/synchronizer/mod.rs (L396-422)
```rust
        match message {
            packed::SyncMessageUnionReader::GetHeaders(reader) => {
                tokio::task::block_in_place(|| {
                    GetHeadersProcess::new(reader, self, peer, &nc).execute()
                })
            }
            packed::SyncMessageUnionReader::SendHeaders(reader) => {
                tokio::task::block_in_place(|| {
                    HeadersProcess::new(reader, self, peer, &nc).execute()
                })
            }
            packed::SyncMessageUnionReader::GetBlocks(reader) => {
                tokio::task::block_in_place(|| {
                    GetBlocksProcess::new(reader, self, peer, &nc).execute()
                })
            }
            packed::SyncMessageUnionReader::SendBlock(reader) => {
                if reader.check_data() {
                    BlockProcess::new(reader, self, peer, nc).execute()
                } else {
                    StatusCode::ProtocolMessageIsMalformed.with_context("SendBlock is invalid")
                }
            }
            packed::SyncMessageUnionReader::InIBD(_) => {
                InIBDProcess::new(self, peer, &nc).execute().await
            }
        }
```

**File:** sync/src/synchronizer/get_headers_process.rs (L53-65)
```rust
        if active_chain.is_initial_block_download() {
            info!(
                "Ignoring getheaders from peer={} because the node is in initial block download stage.",
                self.peer
            );
            self.send_in_ibd();
            let shared = self.synchronizer.shared();
            if let Some(flag) = shared.state().peers().get_flag(self.peer)
                && (flag.is_outbound || flag.is_whitelist || flag.is_protect)
            {
                shared.insert_peer_unknown_header_list(self.peer, block_locator_hashes);
            };
            return Status::ignored();
```

**File:** sync/src/synchronizer/get_blocks_process.rs (L33-45)
```rust
    pub fn execute(self) -> Status {
        let block_hashes = self.message.block_hashes();
        // use MAX_HEADERS_LEN as limit, we may increase the value of INIT_BLOCKS_IN_TRANSIT_PER_PEER in the future
        if block_hashes.len() > MAX_HEADERS_LEN {
            return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                "BlockHashes count({}) > MAX_HEADERS_LEN({})",
                block_hashes.len(),
                MAX_HEADERS_LEN,
            ));
        }
        let active_chain = self.synchronizer.shared.active_chain();

        let iter = block_hashes.iter().take(INIT_BLOCKS_IN_TRANSIT_PER_PEER);
```
