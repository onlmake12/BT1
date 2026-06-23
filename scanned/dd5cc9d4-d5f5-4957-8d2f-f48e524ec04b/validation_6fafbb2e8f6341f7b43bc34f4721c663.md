### Title
Missing IBD State Check in `GetBlocksProcess` Allows Inbound Peers to Bypass IBD Restrictions — (File: `sync/src/synchronizer/get_blocks_process.rs`)

---

### Summary

The `GetBlocksProcess::execute()` handler does not check `is_initial_block_download()` before serving blocks to requesting peers. The module-level documentation for the synchronizer explicitly states that a node in IBD mode must respond with `InIBD` to **both** `GetHeaders` and `GetBlocks` requests. `GetHeadersProcess` correctly implements this guard, but `GetBlocksProcess` is missing it entirely, allowing any connected inbound peer to bypass the IBD restriction and force the node to serve full block data during the critical IBD phase.

---

### Finding Description

The synchronizer module's own documentation states the intended invariant:

> "When CKB node is in IBD mode, it will respond `packed::InIBD` to `GetHeaders` and `GetBlocks` requests"

`GetHeadersProcess::execute()` correctly enforces this:

```rust
// sync/src/synchronizer/get_headers_process.rs:53-65
if active_chain.is_initial_block_download() {
    info!(
        "Ignoring getheaders from peer={} because the node is in initial block download stage.",
        self.peer
    );
    self.send_in_ibd();
    // ... additional outbound peer handling ...
    return Status::ignored();
}
```

`GetBlocksProcess::execute()` has no such check:

```rust
// sync/src/synchronizer/get_blocks_process.rs:33-97
pub fn execute(self) -> Status {
    let block_hashes = self.message.block_hashes();
    // ... size check only ...
    let active_chain = self.synchronizer.shared.active_chain();
    // NO is_initial_block_download() check
    for block_hash in iter {
        // ... serves blocks unconditionally ...
        if let Some(block) = active_chain.get_block(&block_hash) {
            // sends SendBlock response
        }
    }
    Status::ok()
}
```

The IBD check is not performed at a higher dispatch level either — `GetHeadersProcess` would not need its own internal check if there were a top-level guard. There is no rate limiter on the sync protocol's `GetBlocks` path (the rate limiter in `Relayer::try_process` applies only to relay messages, not sync messages).

---

### Impact Explanation

During IBD, the node is bandwidth- and CPU-constrained, downloading and verifying the entire chain history from trusted outbound peers. Any connected inbound peer can send `GetBlocks` with up to `MAX_HEADERS_LEN` (2000) block hashes per message. The node will:

1. Retrieve and serialize each requested block from its local store.
2. Transmit full block data back to the requesting peer.
3. Never send `InIBD`, so the peer has no signal to stop and can repeat indefinitely.

This allows an unprivileged inbound peer to consume the node's outbound bandwidth and I/O during IBD, directly competing with the node's own block download and verification pipeline and slowing IBD completion. The node also serves potentially incomplete chain data to peers that should instead be told the node is not yet ready.

---

### Likelihood Explanation

Any peer that establishes an inbound connection to a CKB node in IBD can immediately send `GetBlocks` messages. No special privilege, key, or majority hashpower is required. The attacker-controlled entry path is: establish TCP connection → complete Tentacle handshake → send `SyncMessage::GetBlocks` with a list of known block hashes. This is reachable by any unprivileged network peer.

---

### Recommendation

Add the IBD guard to `GetBlocksProcess::execute()`, mirroring `GetHeadersProcess`:

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
+       // send InIBD response
+       return Status::ignored();
+   }

    // ... existing block-serving logic ...
}
```

---

### Proof of Concept

1. Node A is in IBD (tip is far behind the network).
2. Attacker peer B establishes an inbound connection to Node A.
3. B sends `SyncMessage::GetBlocks` with a list of 2000 known block hashes (e.g., genesis through block 2000).
4. Node A's `GetBlocksProcess::execute()` iterates the list, retrieves each block from RocksDB, serializes it, and sends `SendBlock` responses — no IBD check fires.
5. B repeats continuously. Node A's outbound bandwidth is consumed serving B while its own IBD download pipeline is starved.
6. Contrast: if B sends `SyncMessage::GetHeaders`, Node A immediately returns `InIBD` and ignores the request, as intended. [1](#0-0) [2](#0-1) [3](#0-2)

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
