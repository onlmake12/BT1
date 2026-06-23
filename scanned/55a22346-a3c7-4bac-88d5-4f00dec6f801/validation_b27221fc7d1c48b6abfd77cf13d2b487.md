### Title
IBD State Guard Missing in `GetBlocksProcess` While Present in `GetHeadersProcess` — (File: `sync/src/synchronizer/get_blocks_process.rs`)

---

### Summary

The CKB synchronizer module explicitly documents that a node in Initial Block Download (IBD) mode must respond with `InIBD` to **both** `GetHeaders` and `GetBlocks` requests. The `GetHeadersProcess` handler correctly enforces this guard, but `GetBlocksProcess` has no IBD check at all. Any unprivileged inbound peer can send `GetBlocks` to an IBD node and receive full serialized block data in response, violating the IBD isolation policy and wasting the syncing node's bandwidth and CPU.

---

### Finding Description

The module-level documentation in `sync/src/synchronizer/mod.rs` explicitly states the intended behavior:

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

`GetBlocksProcess::execute()` has **no such check**. It proceeds directly to serve blocks to any requesting peer regardless of IBD state:

```rust
pub fn execute(self) -> Status {
    let block_hashes = self.message.block_hashes();
    // ... only checks: count limit, genesis, dedup, BLOCK_VALID status
    // NO is_initial_block_download() check
    if let Some(block) = active_chain.get_block(&block_hash) {
        // sends full block to peer
    }
}
``` [3](#0-2) 

Both handlers are dispatched from the same `try_process` match arm in the `Synchronizer`: [4](#0-3) 

The `is_initial_block_download()` function is available on `active_chain` and is already used in `GetHeadersProcess`, `HeadersProcess`, and the notify loop — but not in `GetBlocksProcess`. [5](#0-4) 

---

### Impact Explanation

During IBD, the node is supposed to focus exclusively on syncing with one selected outbound/whitelist peer and stop responding to most P2P requests. By bypassing this guard, an unprivileged inbound peer can:

1. **Force the IBD node to serialize and transmit full block bodies** for any valid block hash it requests, consuming upload bandwidth and CPU that should be dedicated to the node's own sync.
2. **Violate the IBD isolation policy** — the node is effectively serving as a full block server to arbitrary peers while it has not yet verified its own chain.
3. **Amplify the effect** by sending up to `MAX_HEADERS_LEN` (2048) block hashes per message, each triggering a full block serialization and async send. [6](#0-5) 

---

### Likelihood Explanation

- **Attacker requirement**: Any peer that can establish an inbound connection to the IBD node. IBD nodes accept inbound connections normally.
- **Knowledge required**: Block hashes are public and can be obtained from any synced node or block explorer.
- **Effort**: Trivial — send a `GetBlocks` message with known block hashes over the Sync protocol.
- **No privilege required**: No key, no operator access, no majority hashpower.

---

### Recommendation

Add the IBD guard to `GetBlocksProcess::execute()`, mirroring the pattern in `GetHeadersProcess`:

```rust
pub fn execute(self) -> Status {
    let active_chain = self.synchronizer.shared.active_chain();

    if active_chain.is_initial_block_download() {
        // optionally send InIBD response
        return Status::ignored();
    }
    // ... rest of existing logic
}
```

This matches the documented contract and the existing pattern in `GetHeadersProcess`. [2](#0-1) 

---

### Proof of Concept

1. Start a CKB node from genesis (it will be in IBD mode since its tip timestamp is far behind wall clock).
2. Connect an inbound peer to it.
3. From the inbound peer, send a `SyncMessage::GetBlocks` containing hashes of known blocks (e.g., obtained from a public node).
4. Observe that the IBD node responds with `SendBlock` messages containing full block data, without sending `InIBD`.
5. Compare with `GetHeaders`: sending `GetHeaders` to the same IBD node correctly returns `InIBD` and ignores the request.

The asymmetry is directly visible in the dispatch table at `sync/src/synchronizer/mod.rs` lines 396–422: `GetHeaders` goes through `GetHeadersProcess` which has the IBD guard; `GetBlocks` goes through `GetBlocksProcess` which does not. [4](#0-3)

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
