Audit Report

## Title
Missing IBD State Check in `GetBlocksProcess::execute()` Allows Peers to Bypass IBD Restriction — (File: `sync/src/synchronizer/get_blocks_process.rs`)

## Summary
The module-level documentation for `sync/src/synchronizer/mod.rs` explicitly states that a node in IBD must respond with `packed::InIBD` to both `GetHeaders` and `GetBlocks` requests. `GetHeadersProcess::execute()` correctly enforces this guard, but `GetBlocksProcess::execute()` contains no `is_initial_block_download()` check, allowing any inbound peer to elicit full block data from a node during IBD, contrary to the documented and intended protocol invariant.

## Finding Description
The documented invariant is stated at `sync/src/synchronizer/mod.rs` lines 1–7: [1](#0-0) 

`GetHeadersProcess::execute()` enforces this at lines 53–66 via `active_chain.is_initial_block_download()`, calling `self.send_in_ibd()` and returning `Status::ignored()`: [2](#0-1) 

`GetBlocksProcess::execute()` performs only a size check and then unconditionally iterates and serves blocks. There is no `is_initial_block_download()` call anywhere in the function: [3](#0-2) 

The dispatch in `Synchronizer::try_process()` routes `GetBlocks` directly to `GetBlocksProcess` with no top-level IBD guard: [4](#0-3) 

There is a partial natural mitigation: the `contains_block_status(..., BlockStatus::BLOCK_VALID)` check at line 60 means only already-validated blocks are served. However, during IBD the node has validated all blocks from genesis up to its current sync point — which can be a large range of blocks — so the attack surface is non-trivial and grows as IBD progresses. The `INIT_BLOCKS_IN_TRANSIT_PER_PEER` cap limits blocks per request but does not prevent repeated requests. [5](#0-4) 

## Impact Explanation
Any inbound peer can repeatedly send `SyncMessage::GetBlocks` with up to `INIT_BLOCKS_IN_TRANSIT_PER_PEER` known block hashes (e.g., genesis through any already-validated block). The node will retrieve each block from RocksDB, serialize it, and transmit it — never sending `InIBD` as the protocol requires. This consumes outbound bandwidth and disk I/O that the node needs for its own IBD download and verification pipeline, slowing IBD completion. The node also violates the protocol contract by serving data to peers that should instead receive `InIBD`. This maps to **Low (501–2000 points): Any other important performance improvements for CKB**, as the impact is resource consumption on a single node during IBD rather than a network-wide crash or consensus deviation.

## Likelihood Explanation
No special privilege is required. Any peer that completes the Tentacle handshake and establishes an inbound connection can immediately send `SyncMessage::GetBlocks`. Block hashes from genesis onward are publicly known. The attack path is: establish TCP connection → complete Tentacle handshake → send `SyncMessage::GetBlocks` with known block hashes → repeat. This is fully reachable by any unprivileged network participant.

## Recommendation
Add the IBD guard to `GetBlocksProcess::execute()` immediately after the size check, mirroring `GetHeadersProcess`:

```rust
pub fn execute(self) -> Status {
    let block_hashes = self.message.block_hashes();
    if block_hashes.len() > MAX_HEADERS_LEN {
        return StatusCode::ProtocolMessageIsMalformed.with_context(...);
    }
    let active_chain = self.synchronizer.shared.active_chain();

+   if active_chain.is_initial_block_download() {
+       debug!(
+           "Ignoring getblocks from peer={} because node is in IBD.",
+           self.peer
+       );
+       // send InIBD response (reuse the send_in_ibd pattern from GetHeadersProcess)
+       return Status::ignored();
+   }

    // ... existing block-serving logic ...
}
```

A `send_in_ibd` helper should be added to `GetBlocksProcess` analogous to the one in `GetHeadersProcess` at lines 101–115. [6](#0-5) 

## Proof of Concept
1. Start a CKB node (Node A) from genesis so it enters IBD.
2. From a second host, establish an inbound TCP connection to Node A and complete the Tentacle/Sync protocol handshake.
3. Send a `SyncMessage::GetBlocks` message containing the hashes of blocks 1–16 (genesis+1 through genesis+16, all publicly known and validated early in IBD).
4. Observe that Node A responds with `SendBlock` messages for each requested block rather than `InIBD`.
5. Contrast: send `SyncMessage::GetHeaders` to Node A — it immediately returns `InIBD` and logs the ignore message, as intended.
6. Repeat step 3 in a loop from multiple connections; measure Node A's outbound bandwidth and RocksDB read I/O increasing while its own IBD download rate decreases.

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

**File:** sync/src/synchronizer/mod.rs (L407-411)
```rust
            packed::SyncMessageUnionReader::GetBlocks(reader) => {
                tokio::task::block_in_place(|| {
                    GetBlocksProcess::new(reader, self, peer, &nc).execute()
                })
            }
```

**File:** sync/src/synchronizer/get_headers_process.rs (L53-66)
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
        }
```

**File:** sync/src/synchronizer/get_headers_process.rs (L101-115)
```rust
    fn send_in_ibd(&self) {
        let content = packed::InIBD::new_builder().build();
        let message = packed::SyncMessage::new_builder().set(content).build();
        let nc = Arc::clone(self.nc);
        let peer = self.peer;
        self.synchronizer
            .shared()
            .shared()
            .async_handle()
            .spawn(async move {
                let _ignore =
                    async_send_message(SupportProtocols::Sync.protocol_id(), &nc, peer, &message)
                        .await;
            });
    }
```

**File:** sync/src/synchronizer/get_blocks_process.rs (L33-66)
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

        let mut dedup = HashSet::new();
        for block_hash in iter {
            debug!("get_blocks {} from peer {:?}", block_hash, self.peer);
            let block_hash = block_hash.to_entity();

            if block_hash == self.synchronizer.shared().consensus().genesis_hash() {
                return StatusCode::RequestGenesis.with_context("Request genesis block");
            }

            if !dedup.insert(block_hash.clone()) {
                return StatusCode::RequestDuplicate.with_context("Request duplicate block");
            }

            if !active_chain.contains_block_status(&block_hash, BlockStatus::BLOCK_VALID) {
                debug!(
                    "Ignoring get_block {} request from peer={} as it is not verified.",
                    block_hash, self.peer
                );
                continue;
            }
```
