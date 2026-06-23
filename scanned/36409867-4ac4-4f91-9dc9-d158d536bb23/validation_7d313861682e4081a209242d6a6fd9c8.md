### Title
`GetBlocksProcess` Missing IBD Guard Allows Resource Exhaustion of Syncing Nodes — (`File: sync/src/synchronizer/get_blocks_process.rs`)

---

### Summary

The CKB synchronizer module explicitly documents that a node in Initial Block Download (IBD) mode must respond with `InIBD` to both `GetHeaders` and `GetBlocks` requests. `GetHeadersProcess` correctly enforces this guard, but `GetBlocksProcess` has no IBD check at all. Any unprivileged P2P peer can therefore send `GetBlocks` to an IBD node and force it to perform disk reads, block serialization, and network transmission — consuming resources that should be reserved for the IBD download itself.

---

### Finding Description

The module-level documentation in `sync/src/synchronizer/mod.rs` states:

> "When CKB node is in IBD mode, it will respond `packed::InIBD` to `GetHeaders` and `GetBlocks` requests" [1](#0-0) 

`GetHeadersProcess::execute()` faithfully implements this contract. At line 53 it checks `active_chain.is_initial_block_download()`, sends an `InIBD` response, and returns early: [2](#0-1) 

`GetBlocksProcess::execute()` contains **no such check**. It validates only message size, genesis-block requests, duplicates, and `BlockStatus::BLOCK_VALID`, then immediately reads and sends blocks: [3](#0-2) 

The top-level `Synchronizer::received()` dispatcher performs no IBD gate before calling `self.process(...)`, so `GetBlocksProcess` is reached unconditionally: [4](#0-3) 

The relayer protocol does apply a top-level IBD guard, but that guard is in `sync/src/relayer/mod.rs` and covers only relay messages — it does not protect the sync protocol's `GetBlocks` path: [5](#0-4) 

---

### Impact Explanation

An attacker who connects to a node that is in IBD mode can repeatedly send `GetBlocks` messages requesting up to `MAX_HEADERS_LEN` block hashes per message. For each valid hash the node will:

1. Perform a RocksDB read to retrieve the full block.
2. Serialize the block into a `SendBlock` protobuf message.
3. Transmit the message over the P2P connection.

This consumes disk I/O, CPU, and outbound bandwidth that the IBD node needs for its own block download. With multiple concurrent connections each flooding `GetBlocks`, the IBD process can be significantly slowed or stalled. The node cannot distinguish this traffic from legitimate sync traffic because no IBD gate exists at the `GetBlocks` handler level.

The impact is **resource exhaustion / IBD disruption**, not a consensus or fund-safety issue.

---

### Likelihood Explanation

IBD is the normal startup state for any new or long-offline CKB node. Any peer that can establish a P2P connection — which requires no privilege — can immediately begin sending `GetBlocks` messages. No special knowledge, key, or majority hash power is required. The attack is trivially repeatable and can be automated.

---

### Recommendation

Add an IBD guard at the top of `GetBlocksProcess::execute()`, mirroring the pattern already used in `GetHeadersProcess`:

```rust
// In GetBlocksProcess::execute()
let active_chain = self.synchronizer.shared.active_chain();
if active_chain.is_initial_block_download() {
    // send InIBD response, matching GetHeadersProcess behavior
    self.send_in_ibd();
    return Status::ignored();
}
```

Alternatively, add a single IBD gate in the `Synchronizer::process()` dispatch function that covers both `GetHeaders` and `GetBlocks` message types, removing the per-handler duplication.

---

### Proof of Concept

1. Start a CKB node from genesis (or with a stale tip). Confirm it is in IBD (`is_initial_block_download() == true`).
2. Connect a custom P2P client as an inbound peer.
3. Send a `SyncMessage::GetBlocks` containing hashes of known-valid early blocks (e.g., blocks 1–16).
4. Observe that the node responds with `SendBlock` messages for each requested block — **no `InIBD` response is sent**.
5. Repeat in a tight loop from multiple connections; measure the IBD node's disk I/O and outbound bandwidth increasing while its own block-download rate decreases.

The root cause is the absence of `active_chain.is_initial_block_download()` in `GetBlocksProcess::execute()` at `sync/src/synchronizer/get_blocks_process.rs`, contrasted with its presence in `GetHeadersProcess::execute()` at `sync/src/synchronizer/get_headers_process.rs` line 53. [6](#0-5) [2](#0-1)

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
