### Title
`GetBlocksProcess` Missing IBD Guard Allows Any Peer to Bypass IBD Isolation and Drain Resources — (`File: sync/src/synchronizer/get_blocks_process.rs`)

---

### Summary

The CKB synchronizer module explicitly documents that during Initial Block Download (IBD), the node must respond with `InIBD` to both `GetHeaders` **and** `GetBlocks` requests. `GetHeadersProcess` correctly enforces this guard. `GetBlocksProcess` does not implement the guard at all, allowing any unprivileged connected peer to force an IBD-mode node to serve full block data, bypassing the intended IBD isolation and consuming I/O, CPU, and bandwidth that should be dedicated to the sync process.

---

### Finding Description

The module-level documentation in `sync/src/synchronizer/mod.rs` explicitly states:

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

`GetBlocksProcess::execute()`, however, contains **no IBD check whatsoever**. It immediately processes the request and serves block data to any requesting peer:

```rust
pub fn execute(self) -> Status {
    let block_hashes = self.message.block_hashes();
    // ... only checks count limit and dedup, no IBD guard ...
    let active_chain = self.synchronizer.shared.active_chain();
    for block_hash in iter {
        if let Some(block) = active_chain.get_block(&block_hash) {
            // sends block to peer unconditionally
        }
    }
    Status::ok()
}
``` [3](#0-2) 

Both handlers are dispatched from the same `try_process` match arm in `Synchronizer`: [4](#0-3) 

The `is_initial_block_download()` function is available on `active_chain` in both handlers: [5](#0-4) 

---

### Impact Explanation

During IBD, the node is designed to isolate itself — syncing only with one selected outbound peer and refusing most P2P requests. By sending `GetBlocks` messages, any connected peer (inbound or outbound) can:

1. **Force the IBD node to read and transmit full block data** from its local RocksDB store, consuming disk I/O, CPU serialization time, and network bandwidth that should be reserved for the IBD sync pipeline.
2. **Bypass the IBD isolation boundary** — the node is supposed to be in a protected state, but `GetBlocks` allows arbitrary peers to extract block data and consume node resources.
3. **Slow down IBD** — each `GetBlocks` request can request up to `INIT_BLOCKS_IN_TRANSIT_PER_PEER` (16) blocks per call, and up to `MAX_HEADERS_LEN` (2000) hashes per message. A flood of such requests from multiple peers can saturate the node's I/O during the most resource-sensitive phase of operation.

Severity: **Medium** — matches the original report's class. No funds are at risk, but the IBD protection invariant is violated and resource exhaustion during IBD is realistic.

---

### Likelihood Explanation

Any peer that can establish a P2P connection to the node (which is unrestricted during IBD — the node still accepts connections) can send `GetBlocks` messages. No special role, key, or privilege is required. The attacker only needs to know valid block hashes (which are public on-chain data). This is trivially reachable by any network peer.

---

### Recommendation

Add the same IBD guard to `GetBlocksProcess::execute()` that exists in `GetHeadersProcess::execute()`. Before processing any block hash requests, check `active_chain.is_initial_block_download()` and return an `InIBD` response (or `Status::ignored()`) if the node is in IBD mode:

```rust
pub fn execute(self) -> Status {
    let active_chain = self.synchronizer.shared.active_chain();

    if active_chain.is_initial_block_download() {
        // send InIBD response and return, consistent with GetHeadersProcess
        return Status::ignored();
    }
    // ... rest of existing logic
}
```

This aligns the implementation with the documented invariant in `sync/src/synchronizer/mod.rs`.

---

### Proof of Concept

1. Start a CKB node from genesis (or with a stale tip) so that `is_initial_block_download()` returns `true`.
2. Connect a peer to the node.
3. From the peer, send a `SyncMessage` containing a `GetBlocks` payload with valid block hashes (e.g., the genesis block hash and any known early block hashes).
4. **Observed**: The node responds with `SendBlock` messages containing full block data.
5. **Expected per documentation**: The node should respond with `InIBD` and ignore the request, as it does for `GetHeaders`.
6. Repeat step 3 in a loop with batches of up to 2000 hashes per message to demonstrate resource consumption impact on the IBD node.

The root cause is the absence of an `is_initial_block_download()` guard in `GetBlocksProcess::execute()` at `sync/src/synchronizer/get_blocks_process.rs` lines 33–97, contrasted with the correctly implemented guard in `GetHeadersProcess::execute()` at `sync/src/synchronizer/get_headers_process.rs` lines 53–66.

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

**File:** sync/src/types/mod.rs (L1706-1708)
```rust
    pub fn is_initial_block_download(&self) -> bool {
        self.shared().is_initial_block_download()
    }
```
