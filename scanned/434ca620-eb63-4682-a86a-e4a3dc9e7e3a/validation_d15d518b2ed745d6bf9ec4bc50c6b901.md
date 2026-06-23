### Title
`GetBlocksProcess` Missing IBD State Guard Allows Peers to Force Block Serving During Initial Block Download - (`File: sync/src/synchronizer/get_blocks_process.rs`)

### Summary

The CKB synchronizer module explicitly documents that during IBD (Initial Block Download), the node should respond with `packed::InIBD` to both `GetHeaders` and `GetBlocks` requests. `GetHeadersProcess` correctly enforces this guard, but `GetBlocksProcess` has no IBD check at all, allowing any connected peer to force an IBD node to serve full blocks while it should be focused exclusively on downloading the chain.

### Finding Description

The module-level comment in `sync/src/synchronizer/mod.rs` states the design intent clearly:

> "When CKB node is in IBD mode, it will respond `packed::InIBD` to `GetHeaders` and `GetBlocks` requests"

`GetHeadersProcess::execute()` correctly implements this:

```rust
if active_chain.is_initial_block_download() {
    info!("Ignoring getheaders from peer={} ...", self.peer);
    self.send_in_ibd();
    ...
    return Status::ignored();
}
``` [1](#0-0) 

`GetBlocksProcess::execute()`, however, contains no such guard. It proceeds directly to serve blocks to the requesting peer:

```rust
pub fn execute(self) -> Status {
    let block_hashes = self.message.block_hashes();
    if block_hashes.len() > MAX_HEADERS_LEN { ... }
    let active_chain = self.synchronizer.shared.active_chain();
    let iter = block_hashes.iter().take(INIT_BLOCKS_IN_TRANSIT_PER_PEER);
    // ... serves blocks with no IBD check
``` [2](#0-1) 

The `Synchronizer::received()` dispatcher has no top-level IBD guard either — it dispatches all messages unconditionally to their respective process handlers. [3](#0-2) 

By contrast, the `Relayer` protocol handler does have a top-level IBD guard that protects all relay messages at once: [4](#0-3) 

The IBD state itself is defined in `shared/src/shared.rs` and is a well-established node-wide condition: [5](#0-4) 

### Impact Explanation

During IBD, the node is supposed to focus all resources on downloading and verifying the chain from a single selected peer. By sending `GetBlocks` messages with up to `MAX_HEADERS_LEN` (2000) block hashes per message, any connected peer can force the IBD node to:

1. Look up each requested block hash in the chain store.
2. Serialize and transmit full block data for every `BLOCK_VALID` block it has already verified.

This wastes upload bandwidth and CPU on the IBD node during the most resource-intensive phase of its lifecycle, potentially slowing down or stalling the IBD process. It also violates the stated protocol invariant that IBD nodes do not serve `GetBlocks` requests.

### Likelihood Explanation

Likelihood is high. Any peer that establishes a connection to the node can send a `GetBlocks` message. No privilege, key, or special role is required. The attacker-controlled entry path is: connect to the target node → send a `packed::SyncMessage` containing `GetBlocks` with valid block hashes → the IBD node serves the blocks without checking its own IBD state.

### Recommendation

Add an IBD guard at the top of `GetBlocksProcess::execute()`, mirroring the pattern used in `GetHeadersProcess`:

```rust
pub fn execute(self) -> Status {
    let active_chain = self.synchronizer.shared.active_chain();
    if active_chain.is_initial_block_download() {
        // Optionally send InIBD response here
        return Status::ignored();
    }
    // ... rest of existing logic
}
```

Alternatively, add a top-level IBD check in `Synchronizer::received()` before dispatching to any process handler, similar to how `Relayer::received()` handles it.

### Proof of Concept

1. Start a CKB node from genesis so it enters IBD (`is_initial_block_download()` returns `true`).
2. Connect a peer to the node.
3. From the peer, send a `packed::SyncMessage` containing a `GetBlocks` payload with hashes of known valid blocks (e.g., genesis block hash and any other stored valid block hashes).
4. Observe that the IBD node responds with `SendBlock` messages containing full block data, instead of responding with `InIBD` or ignoring the request.
5. Repeat at high frequency to consume the IBD node's upload bandwidth and CPU, slowing its chain synchronization.

The inconsistency is directly visible by comparing `get_headers_process.rs` (IBD check present at line 53) with `get_blocks_process.rs` (no IBD check anywhere in `execute()`). [2](#0-1)

### Citations

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

**File:** sync/src/synchronizer/mod.rs (L890-970)
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
        debug!(
            "Process message={}, peer={}, cost={:?}",
            msg.item_name(),
            peer_index,
            Instant::now().saturating_duration_since(start_time),
        );
    }
```

**File:** sync/src/relayer/mod.rs (L815-818)
```rust
        // If self is in the IBD state, don't process any relayer message.
        if self.shared.active_chain().is_initial_block_download() {
            return;
        }
```

**File:** shared/src/shared.rs (L382-394)
```rust
    pub fn is_initial_block_download(&self) -> bool {
        // Once this function has returned false, it must remain false.
        if self.ibd_finished.load(Ordering::Acquire) {
            false
        } else if unix_time_as_millis().saturating_sub(self.snapshot().tip_header().timestamp())
            > MAX_TIP_AGE
        {
            true
        } else {
            self.ibd_finished.store(true, Ordering::Release);
            false
        }
    }
```
