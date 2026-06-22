### Title
Inconsistent IBD Guard Enforcement Allows Block Serving During Initial Block Download — (File: `sync/src/synchronizer/get_blocks_process.rs`)

### Summary
The `GetBlocksProcess::execute` function lacks the IBD (Initial Block Download) guard that `GetHeadersProcess::execute` enforces. The module documentation explicitly states both `GetHeaders` and `GetBlocks` should be rejected with an `InIBD` response during IBD, but only `GetHeaders` is guarded. Any unprivileged connected peer can request and receive blocks from a node in IBD mode, undermining the IBD isolation mechanism and wasting resources during the critical sync phase.

### Finding Description
The module-level documentation in `sync/src/synchronizer/mod.rs` explicitly states:

> "When CKB node is in IBD mode, it will respond `packed::InIBD` to `GetHeaders` and `GetBlocks` requests"

`GetHeadersProcess::execute` correctly implements this guard:

```rust
if active_chain.is_initial_block_download() {
    info!("Ignoring getheaders from peer={} because the node is in initial block download stage.", self.peer);
    self.send_in_ibd();
    // ...
    return Status::ignored();
}
``` [1](#0-0) 

However, `GetBlocksProcess::execute` has **no IBD check at all**. It proceeds directly to serve blocks to any requesting peer:

```rust
pub fn execute(self) -> Status {
    let block_hashes = self.message.block_hashes();
    // ...
    let active_chain = self.synchronizer.shared.active_chain();
    // No is_initial_block_download() check here
    for block_hash in iter {
        // ...
        if let Some(block) = active_chain.get_block(&block_hash) {
            // sends block to peer
        }
    }
    Status::ok()
}
``` [2](#0-1) 

The `Synchronizer::received` handler dispatches both message types without any top-level IBD guard, so the per-handler check is the only enforcement point. [3](#0-2) 

The module comment documents the intended symmetric behavior: [4](#0-3) 

### Impact Explanation
During IBD, the node is under heavy load downloading and verifying the entire chain history. An unprivileged peer can send `GetBlocks` messages and force the IBD node to:
1. Look up block hashes and retrieve full block bodies from the store
2. Serialize and transmit those blocks over the network

This wastes CPU and bandwidth at the worst possible time. It also creates an inconsistent state: the node refuses to serve headers (sending `InIBD`) but silently serves full blocks, contradicting the documented IBD isolation guarantee. The `is_initial_block_download()` function is a one-way latch — once IBD ends it stays false — so the window is the entire IBD phase. [5](#0-4) 

### Likelihood Explanation
The entry path requires only a standard P2P connection. Any peer that connects to the node during IBD can immediately send `GetBlocks` messages with arbitrary block hashes. No authentication, no privilege, and no special state is required. The `GetBlocksProcess` imposes a per-request cap of `INIT_BLOCKS_IN_TRANSIT_PER_PEER` hashes, but an attacker can send repeated messages at the rate limit. [6](#0-5) 

### Recommendation
Add an IBD guard to `GetBlocksProcess::execute`, mirroring the pattern in `GetHeadersProcess`:

```rust
pub fn execute(self) -> Status {
    // ...
    let active_chain = self.synchronizer.shared.active_chain();

+   if active_chain.is_initial_block_download() {
+       // Optionally send InIBD response here
+       return Status::ignored();
+   }

    let iter = block_hashes.iter().take(INIT_BLOCKS_IN_TRANSIT_PER_PEER);
    // ...
}
```

This aligns the implementation with the documented invariant that both `GetHeaders` and `GetBlocks` are suppressed during IBD.

### Proof of Concept
1. Start a CKB node from a clean state (tip timestamp far behind wall clock → IBD mode active, `is_initial_block_download()` returns `true`).
2. Connect a peer to the node via the Sync protocol.
3. Send a `packed::SyncMessage` containing a `GetBlocks` payload with valid block hashes (e.g., the genesis block hash or any known block hash).
4. Observe that the node responds with `SendBlock` messages containing full block data, rather than an `InIBD` response.
5. Contrast: sending a `GetHeaders` message to the same node returns `InIBD` and is ignored.

The asymmetry confirms that `GetBlocksProcess` bypasses the IBD guard that `GetHeadersProcess` enforces. [2](#0-1) [7](#0-6)

### Citations

**File:** sync/src/synchronizer/get_headers_process.rs (L36-99)
```rust
    pub fn execute(self) -> Status {
        let active_chain = self.synchronizer.shared.active_chain();

        let block_locator_hashes = self
            .message
            .block_locator_hashes()
            .iter()
            .map(|x| x.to_entity())
            .collect::<Vec<Byte32>>();
        let hash_stop = self.message.hash_stop().to_entity();
        let locator_size = block_locator_hashes.len();
        if locator_size > MAX_LOCATOR_SIZE {
            return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                "Locator count({locator_size}) > MAX_LOCATOR_SIZE({MAX_LOCATOR_SIZE})"
            ));
        }

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

        if let Some(block_number) =
            active_chain.locate_latest_common_block(&hash_stop, &block_locator_hashes[..])
        {
            debug!(
                "headers latest_common={} tip={} begin",
                block_number,
                active_chain.tip_header().number(),
            );

            self.synchronizer.peers().getheaders_received(self.peer);
            let headers: Vec<core::HeaderView> =
                active_chain.get_locator_response(block_number, &hash_stop);
            // response headers

            debug!("headers len={}", headers.len());

            let content = packed::SendHeaders::new_builder()
                .headers(headers.into_iter().map(|x| x.data()).collect::<Vec<_>>())
                .build();
            let message = packed::SyncMessage::new_builder().set(content).build();
            let nc = Arc::clone(self.nc);
            self.synchronizer
                .shared()
                .shared()
                .async_handle()
                .spawn(async move { async_send_message_to(&nc, self.peer, &message).await });
        } else {
            return StatusCode::GetHeadersMissCommonAncestors
                .with_context(format!("{block_locator_hashes:#x?}"));
        }
        Status::ok()
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
