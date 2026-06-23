### Title
Missing IBD State Check in `GetBlocksProcess` Allows Block Serving During Initial Block Download - (File: `sync/src/synchronizer/get_blocks_process.rs`)

### Summary

The `GetBlocksProcess::execute()` function in the CKB synchronizer does not check whether the local node is in Initial Block Download (IBD) mode before serving blocks to remote peers. The module's own documentation explicitly states that the node should respond with `InIBD` to both `GetHeaders` and `GetBlocks` requests during IBD, but only `GetHeadersProcess` implements this guard. Any unprivileged peer can exploit this omission to force a node in IBD to perform disk reads and network sends for arbitrary block hashes.

### Finding Description

The synchronizer module comment at the top of `sync/src/synchronizer/mod.rs` states:

> "When CKB node is in IBD mode, it will respond `packed::InIBD` to `GetHeaders` and `GetBlocks` requests"

`GetHeadersProcess::execute()` correctly implements this contract: [1](#0-0) 

It checks `active_chain.is_initial_block_download()`, sends an `InIBD` response, and returns `Status::ignored()`.

`GetBlocksProcess::execute()` has no such check. It proceeds directly to iterate over requested block hashes, look them up in the chain store, and spawn async tasks to send each found block back to the requesting peer: [2](#0-1) 

Both handlers are dispatched from the same `try_process` match arm in `Synchronizer`: [3](#0-2) 

The `Relayer` protocol handler does apply a top-level IBD guard: [4](#0-3) 

But the `Synchronizer`'s `received` handler applies no such top-level guard, delegating the responsibility to each individual process handler — and `GetBlocksProcess` omits it.

The IBD state itself is computed in `Shared::is_initial_block_download()`: [5](#0-4) 

### Impact Explanation

During IBD a node is supposed to synchronize exclusively with one selected peer and ignore most inbound P2P service requests. By sending `GetBlocks` messages to a node in IBD, any peer can:

1. Force the node to perform repeated RocksDB block lookups (`active_chain.get_block`) for up to `INIT_BLOCKS_IN_TRANSIT_PER_PEER` hashes per message.
2. Force the node to spawn async send tasks that consume network bandwidth uploading full blocks.
3. Compete with the node's own IBD download traffic for I/O and CPU, slowing or stalling IBD completion.

The node's own documentation contract (respond with `InIBD`, not with blocks) is violated, so peers that correctly implement the protocol will also receive unexpected `SendBlock` frames instead of the `InIBD` signal they should use to adjust their sync strategy.

### Likelihood Explanation

The entry path requires only a standard P2P connection. Any unprivileged peer that connects to a CKB node can send a `GetBlocks` sync message. No special privileges, keys, or majority hash power are needed. Nodes are in IBD for an extended period after first startup (until the tip timestamp is within `MAX_TIP_AGE` of wall clock), making the window of exposure long and predictable.

### Recommendation

Add an IBD guard at the top of `GetBlocksProcess::execute()`, mirroring the pattern in `GetHeadersProcess`:

```rust
pub fn execute(self) -> Status {
    let active_chain = self.synchronizer.shared.active_chain();

    if active_chain.is_initial_block_download() {
        // Send InIBD and ignore the request, consistent with GetHeadersProcess
        self.send_in_ibd();
        return Status::ignored();
    }

    let block_hashes = self.message.block_hashes();
    // ... rest of existing logic
}
```

A `send_in_ibd` helper analogous to the one in `GetHeadersProcess` should be added to `GetBlocksProcess`.

### Proof of Concept

1. Start a fresh CKB node. Confirm it is in IBD (`get_blockchain_info` → `is_initial_block_download: true`).
2. Connect a custom peer using the Sync protocol.
3. Send a `GetBlocks` sync message containing the genesis block hash or any known early block hash.
4. Observe that the node responds with a `SendBlock` message containing the full block, instead of an `InIBD` message.
5. Repeat at high frequency; observe increased disk I/O and outbound bandwidth on the IBD node, and degraded IBD progress compared to a baseline without the attacker peer.

The contrast with `GetHeadersProcess` is direct: sending a `GetHeaders` message to the same IBD node returns `InIBD` and logs "Ignoring getheaders from peer=… because the node is in initial block download stage." [6](#0-5) [7](#0-6) [8](#0-7)

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
