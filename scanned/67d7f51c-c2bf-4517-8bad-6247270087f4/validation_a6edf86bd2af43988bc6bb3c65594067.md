### Title
`GetBlocksProcess` Bypasses IBD Guard, Serving Blocks to Peers During Initial Block Download — (`File: sync/src/synchronizer/get_blocks_process.rs`)

---

### Summary

The CKB synchronizer's module documentation explicitly states that during Initial Block Download (IBD), the node will respond with `packed::InIBD` to both `GetHeaders` **and** `GetBlocks` requests. `GetHeadersProcess` correctly enforces this guard. However, `GetBlocksProcess` contains no IBD check whatsoever, allowing any unprivileged remote peer to bypass the IBD guard and force the node to serve full block bodies during IBD — wasting bandwidth and CPU resources that should be dedicated to syncing.

---

### Finding Description

The module-level documentation in `sync/src/synchronizer/mod.rs` declares the intended invariant:

> When CKB node is in IBD mode, it will respond `packed::InIBD` to `GetHeaders` and `GetBlocks` requests [1](#0-0) 

`GetHeadersProcess::execute()` correctly enforces this by checking `is_initial_block_download()` at the top of its handler, sending an `InIBD` response, and returning early: [2](#0-1) 

`GetBlocksProcess::execute()`, however, contains **no IBD check**. It directly iterates over requested block hashes, looks them up in the local chain, and sends `SendBlock` responses to the requesting peer: [3](#0-2) 

The `Synchronizer::received()` handler also has no top-level IBD guard (unlike `Relayer::received()`, which does): [4](#0-3) 

The `Synchronizer::try_process()` dispatch routes `GetBlocks` directly to `GetBlocksProcess` with no IBD interception: [5](#0-4) 

---

### Impact Explanation

During IBD, a node is supposed to focus all resources on downloading and verifying the chain. Serving block bodies to other peers during this phase:

1. **Wastes bandwidth and CPU**: The node performs block lookups and serializes full block bodies for each `GetBlocks` request, competing with its own IBD download pipeline.
2. **Violates the documented protocol invariant**: Peers that receive blocks from an IBD node may incorrectly treat it as a fully-synced peer, leading to incorrect sync decisions on the requesting side.
3. **Enables resource exhaustion**: Any connected peer can send repeated `GetBlocks` messages (up to `MAX_HEADERS_LEN` hashes per message) and the node will process each one, performing up to `INIT_BLOCKS_IN_TRANSIT_PER_PEER` block lookups and sends per message. [6](#0-5) 

---

### Likelihood Explanation

Any peer that establishes a sync protocol connection can send a `GetBlocks` message. No authentication, privilege, or special state is required. The `Synchronizer::received()` handler processes all incoming sync messages without an IBD gate, so this path is reachable by any unprivileged network peer during the entire IBD phase (which can last hours on mainnet). [7](#0-6) 

---

### Recommendation

Add an IBD guard at the beginning of `GetBlocksProcess::execute()`, mirroring the pattern in `GetHeadersProcess`:

```rust
pub fn execute(self) -> Status {
    let active_chain = self.synchronizer.shared.active_chain();

    if active_chain.is_initial_block_download() {
        // Send InIBD and return, consistent with GetHeadersProcess behavior
        // and the documented invariant in mod.rs
        return Status::ignored();
    }
    // ... rest of existing logic
}
```

This aligns with the documented invariant, the `GetHeadersProcess` pattern, and the `Relayer::received()` top-level IBD guard. [2](#0-1) 

---

### Proof of Concept

1. Start a CKB node from genesis (IBD mode active — `is_initial_block_download()` returns `true`).
2. Connect a peer that has already synced some blocks.
3. From the peer, send a `SyncMessage::GetBlocks` containing hashes of genesis-adjacent blocks that exist in the IBD node's local store.
4. Observe that the IBD node responds with `SendBlock` messages containing full block data, rather than `InIBD`.
5. Contrast with sending `GetHeaders` to the same IBD node — it correctly responds with `InIBD` and ignores the request.

The discrepancy is directly observable at the protocol level. The `GetBlocksProcess` will serve any block with `BlockStatus::BLOCK_VALID` regardless of the node's IBD state. [8](#0-7)

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

**File:** sync/src/synchronizer/mod.rs (L890-963)
```rust
    async fn received(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
        data: Bytes,
    ) {
        let msg = match packed::SyncMessageReader::from_compatible_slice(&data) {
            Ok(msg) => {
                let item = msg.to_enum();
                if let packed::SyncMessageUnionReader::SendBlock(ref reader) = item {
                    if reader.has_extra_fields() || reader.block().count_extra_fields() > 1 {
                        info!(
                            "A malformed message from peer {}: \
                             excessive fields detected in SendBlock",
                            peer_index
                        );
                        nc.ban_peer(
                            peer_index,
                            BAD_MESSAGE_BAN_TIME,
                            String::from(
                                "send us a malformed message: \
                                 too many fields in SendBlock",
                            ),
                        );
                        return;
                    } else {
                        item
                    }
                } else {
                    match packed::SyncMessageReader::from_slice(&data) {
                        Ok(msg) => msg.to_enum(),
                        _ => {
                            info!(
                                "A malformed message from peer {}: \
                                 excessive fields",
                                peer_index
                            );
                            nc.ban_peer(
                                peer_index,
                                BAD_MESSAGE_BAN_TIME,
                                String::from(
                                    "send us a malformed message: \
                                     too many fields",
                                ),
                            );
                            return;
                        }
                    }
                }
            }
            _ => {
                info!("A malformed message from peer {}", peer_index);
                nc.ban_peer(
                    peer_index,
                    BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };

        debug!("Received msg {} from {}", msg.item_name(), peer_index);
        #[cfg(feature = "with_sentry")]
        {
            let sentry_hub = sentry::Hub::current();
            let _scope_guard = sentry_hub.push_scope();
            sentry_hub.configure_scope(|scope| {
                scope.set_tag("p2p.protocol", "synchronizer");
                scope.set_tag("p2p.message", msg.item_name());
            });
        }

        let start_time = Instant::now();
        self.process(nc, peer_index, msg).await;
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
