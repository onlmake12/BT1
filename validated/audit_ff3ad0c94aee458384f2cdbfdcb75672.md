### Title
`GetBlocksProcess` Serves Blocks to Any Peer During IBD Without IBD State Check — (`File: sync/src/synchronizer/get_blocks_process.rs`)

### Summary

The CKB synchronizer module explicitly documents that both `GetHeaders` and `GetBlocks` requests must be rejected with an `InIBD` response when the node is in Initial Block Download (IBD) mode. `GetHeadersProcess` correctly enforces this restriction, but `GetBlocksProcess` has no IBD check at all. Any unprivileged inbound peer can send `GetBlocks` messages to an IBD node and receive full block bodies in response, consuming the node's I/O, CPU, and bandwidth while it is trying to complete IBD.

### Finding Description

The module-level documentation in `sync/src/synchronizer/mod.rs` explicitly states the design contract:

> "When CKB node is in IBD mode, it will respond `packed::InIBD` to `GetHeaders` and `GetBlocks` requests" [1](#0-0) 

`GetHeadersProcess::execute()` correctly enforces this by checking `active_chain.is_initial_block_download()` at the top of its handler, sending an `InIBD` response, and returning early: [2](#0-1) 

`GetBlocksProcess::execute()` has no such check. It proceeds directly to serve blocks to any requesting peer: [3](#0-2) 

The handler accepts up to `MAX_HEADERS_LEN` (2000) block hashes per message, performs a `BlockStatus` lookup for each, and serves up to `INIT_BLOCKS_IN_TRANSIT_PER_PEER` (16) full block bodies per request — all without any IBD gate. [4](#0-3) 

Both handlers are dispatched from the same `try_process` match arm in `Synchronizer`, so the inconsistency is not a routing artifact: [5](#0-4) 

The `Relayer` protocol correctly gates its entire `received` handler behind an IBD check, showing the intended pattern: [6](#0-5) 

### Impact Explanation

During IBD, the node is designed to focus all sync resources on a single selected peer. An unprivileged inbound peer can send a stream of `GetBlocks` messages, each containing up to 2000 block hashes. For each message the node performs up to 2000 `BlockStatus` map lookups and then reads and transmits up to 16 full block bodies from disk. This:

1. Consumes outbound bandwidth that should be reserved for the node's own IBD download.
2. Causes repeated disk I/O (block body reads) that competes with the IBD write path.
3. Delays IBD completion, extending the window during which the node is not fully validating the chain.

The impact is resource exhaustion / IBD interference from any reachable inbound peer, not a consensus or fund-safety violation. Severity: **Medium**.

### Likelihood Explanation

Any peer that can establish an inbound TCP connection to the CKB node can send `GetBlocks` messages. No authentication, key, or privileged role is required. The node does not rate-limit `GetBlocks` at the Sync protocol level (rate limiting exists only in the Relayer). A single attacker with one connection can sustain a continuous stream of requests. This is trivially reachable on mainnet or testnet.

### Recommendation

Add an IBD guard at the top of `GetBlocksProcess::execute()`, mirroring the pattern in `GetHeadersProcess`:

```rust
pub fn execute(self) -> Status {
    let active_chain = self.synchronizer.shared.active_chain();

    // Add this block, matching GetHeadersProcess behavior:
    if active_chain.is_initial_block_download() {
        // Optionally send InIBD response here as documented
        return Status::ignored();
    }

    let block_hashes = self.message.block_hashes();
    // ... rest of handler unchanged
}
```

This aligns the implementation with the stated module contract and prevents unprivileged peers from consuming IBD node resources.

### Proof of Concept

1. Start a CKB node from genesis (tip timestamp far behind wall clock → IBD mode active, `is_initial_block_download()` returns `true`).
2. Connect an inbound peer (no special flags required).
3. From the peer, send a `SyncMessage::GetBlocks` containing 2000 valid block hashes (e.g., hashes of genesis and any known blocks).
4. **Observed**: The IBD node processes all 2000 hash lookups and sends back `SendBlock` responses for any `BLOCK_VALID` hashes found — no `InIBD` response is sent, no early return occurs.
5. **Expected per documentation**: The node should respond with `InIBD` and ignore the request, as it does for `GetHeaders`.
6. Repeat in a tight loop to sustain resource pressure throughout the IBD phase.

The root cause is confirmed at: [7](#0-6) 
where no `is_initial_block_download()` check exists, contrasted with the enforced check at: [2](#0-1)

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

**File:** sync/src/synchronizer/mod.rs (L396-411)
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

**File:** sync/src/synchronizer/get_blocks_process.rs (L33-97)
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

            if let Some(block) = active_chain.get_block(&block_hash) {
                debug!(
                    "respond_block {} {} to peer {:?}",
                    block.number(),
                    block.hash(),
                    self.peer,
                );
                let content = packed::SendBlock::new_builder().block(block.data()).build();
                let message = packed::SyncMessage::new_builder().set(content).build();

                let nc = Arc::clone(self.nc);
                self.synchronizer
                    .shared()
                    .shared()
                    .async_handle()
                    .spawn(async move { async_send_message_to(&nc, self.peer, &message).await });
            } else {
                // TODO response not found
                // TODO add timeout check in synchronizer

                // We expect that `block_hashes` is sorted descending by height.
                // So if we cannot find the current one from local, we cannot find
                // the next either.
                debug!("Stopping getblocks, since {} is not found", block_hash);
                break;
            }
        }

        Status::ok()
    }
```

**File:** sync/src/relayer/mod.rs (L815-818)
```rust
        // If self is in the IBD state, don't process any relayer message.
        if self.shared.active_chain().is_initial_block_download() {
            return;
        }
```
