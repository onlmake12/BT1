### Title
Missing IBD State Guard in `GetBlocksProcess` Allows Block Serving During Initial Block Download — (`File: sync/src/synchronizer/get_blocks_process.rs`)

---

### Summary

The CKB synchronizer module explicitly documents that both `GetHeaders` and `GetBlocks` P2P messages must be rejected with an `InIBD` response when the node is in Initial Block Download (IBD) mode. `GetHeadersProcess` correctly enforces this guard. `GetBlocksProcess` is missing the equivalent IBD check entirely, allowing any unprivileged inbound peer to force a node in IBD to perform block database lookups and serve blocks, violating the IBD isolation design and wasting resources during the most resource-intensive phase of node operation.

---

### Finding Description

The module-level documentation for the synchronizer is unambiguous about the intended behavior:

> "When CKB node is in IBD mode, it will respond `packed::InIBD` to `GetHeaders` and `GetBlocks` requests" [1](#0-0) 

`GetHeadersProcess::execute()` correctly implements this guard at line 53:

```rust
if active_chain.is_initial_block_download() {
    info!("Ignoring getheaders from peer={} ...", self.peer);
    self.send_in_ibd();
    ...
    return Status::ignored();
}
``` [2](#0-1) 

`GetBlocksProcess::execute()` has **no such check**. It proceeds directly to database lookups and block serving for any peer, regardless of IBD state:

```rust
pub fn execute(self) -> Status {
    let block_hashes = self.message.block_hashes();
    if block_hashes.len() > MAX_HEADERS_LEN { ... }
    let active_chain = self.synchronizer.shared.active_chain();
    let iter = block_hashes.iter().take(INIT_BLOCKS_IN_TRANSIT_PER_PEER);
    // No IBD check — proceeds to serve blocks unconditionally
    for block_hash in iter {
        ...
        if let Some(block) = active_chain.get_block(&block_hash) {
            // sends SendBlock response to peer
        }
    }
    Status::ok()
}
``` [3](#0-2) 

Both message types are dispatched from the same `try_process` match arm without any pre-dispatch IBD gate: [4](#0-3) 

The `is_initial_block_download()` function is a monotonic flag — once the node exits IBD it never re-enters — so the check is cheap and well-defined: [5](#0-4) 

---

### Impact Explanation

During IBD, the node is designed to focus all sync resources on a single outbound peer. An attacker-controlled inbound peer can send `GetBlocks` messages containing up to `MAX_HEADERS_LEN` (2000) block hashes. For each hash the node performs:

1. A `BlockStatus` lookup (`contains_block_status`) against the database.
2. A full block retrieval (`get_block`) and async `SendBlock` message dispatch for any hash that resolves to a `BLOCK_VALID` block.

This forces the IBD node to perform repeated RocksDB reads and network I/O on behalf of arbitrary inbound peers, directly competing with the node's own IBD block download and verification pipeline. The IBD isolation invariant — that the node only interacts with its chosen sync peer — is broken. An attacker with multiple connections can amplify this to degrade IBD throughput.

---

### Likelihood Explanation

The `GetBlocks` message is a standard, unauthenticated P2P sync protocol message. Any peer that completes the CKB handshake can send it. No special privilege, key, or majority hashpower is required. The node accepts inbound connections by default, so the attacker entry path requires only establishing a TCP connection to the node's P2P port and sending a well-formed `GetBlocks` message while the target is in IBD.

---

### Recommendation

Add an IBD guard at the top of `GetBlocksProcess::execute()`, mirroring the pattern in `GetHeadersProcess`:

```rust
pub fn execute(self) -> Status {
    let active_chain = self.synchronizer.shared.active_chain();
    if active_chain.is_initial_block_download() {
        // Optionally send InIBD response, then return
        return Status::ignored();
    }
    // ... existing logic
}
```

This aligns the implementation with the documented invariant and with the behavior of `GetHeadersProcess`.

---

### Proof of Concept

1. Start a CKB node from genesis (tip timestamp far behind wall clock → IBD mode active, confirmed by `is_initial_block_download()` returning `true`).
2. Connect as an inbound peer via the P2P port.
3. Send a `SyncMessage::GetBlocks` containing 16 valid block hashes (e.g., the genesis hash repeated or any known hashes).
4. Observe: the node processes the request, performs database lookups, and sends `SendBlock` responses — **without** sending `InIBD` and without ignoring the message.
5. Contrast: send a `SyncMessage::GetHeaders` to the same node in IBD — the node correctly responds with `InIBD` and ignores the request.

The asymmetry between `GetHeaders` (guarded) and `GetBlocks` (unguarded) during IBD is the root cause, directly analogous to the reported `bridgeMessage()`/`bridgeMessageWETH()` missing `ifNotEmergencyState` modifier pattern.

### Citations

**File:** sync/src/synchronizer/mod.rs (L1-5)
```rust
//! CKB node has initial block download phase (IBD mode) like Bitcoin:
//! <https://btcinformation.org/en/glossary/initial-block-download>
//!
//! When CKB node is in IBD mode, it will respond `packed::InIBD` to `GetHeaders` and `GetBlocks` requests
//!
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
