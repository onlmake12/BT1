### Title
Missing IBD State Check in `GetBlocksProcess` Allows Block Serving During Initial Block Download — (File: `sync/src/synchronizer/get_blocks_process.rs`)

### Summary

The `GetBlocksProcess` handler does not check `is_initial_block_download()` before serving blocks to peers, while the module-level documentation explicitly states that the node should respond with `InIBD` to **both** `GetHeaders` and `GetBlocks` requests during IBD. The `GetHeadersProcess` correctly implements this guard, but `GetBlocksProcess` does not, creating an inconsistency that allows any unprivileged P2P peer to consume the IBD node's bandwidth and CPU by sending `GetBlocks` requests.

---

### Finding Description

The `sync/src/synchronizer/mod.rs` module-level comment explicitly documents the intended behavior:

> When CKB node is in IBD mode, it will respond `packed::InIBD` to `GetHeaders` **and** `GetBlocks` requests [1](#0-0) 

`GetHeadersProcess::execute()` correctly implements this contract — it checks `is_initial_block_download()` and sends an `InIBD` response before returning early: [2](#0-1) 

However, `GetBlocksProcess::execute()` contains **no IBD check at all**. It proceeds directly to serve blocks: [3](#0-2) 

The `send_in_ibd()` helper that sends the `packed::InIBD` message exists only in `GetHeadersProcess` and is never called from `GetBlocksProcess`: [4](#0-3) 

The `is_initial_block_download()` flag is a one-way latch — once IBD ends it stays ended — and is the authoritative source of IBD state: [5](#0-4) 

---

### Impact Explanation

During IBD the node is supposed to focus all resources on downloading and verifying the chain from a single selected peer. Because `GetBlocksProcess` skips the IBD guard:

1. **Bandwidth drain**: Any connected peer can request up to `INIT_BLOCKS_IN_TRANSIT_PER_PEER` blocks per message. The node will look up each hash, check `BlockStatus::BLOCK_VALID`, and serve every already-verified block (genesis and early chain blocks are always `BLOCK_VALID`). An attacker can repeat this continuously.
2. **CPU drain**: Each `GetBlocks` message triggers hash lookups, status checks, block reads from RocksDB, and async message sends — all on the IBD node's threads.
3. **Slowed IBD**: Resources consumed serving attacker-controlled peers are unavailable for the node's own block download pipeline, extending the IBD window.
4. **Spec violation**: The node's own documentation promises `InIBD` responses to `GetBlocks` during IBD; peers that rely on this signal to detect IBD nodes receive no such signal.

**Impact: Medium** — no consensus violation, but measurable resource exhaustion and slowed synchronization reachable by any unprivileged peer.

---

### Likelihood Explanation

Any peer that completes the P2P handshake can send `GetBlocks` messages. No special privilege, key, or majority hashpower is required. During IBD the node is actively connected to peers, so the attack surface is always open for the full duration of IBD. The attacker only needs to know a few valid block hashes (e.g., the genesis hash is public), and the node will serve those blocks.

**Likelihood: Medium** — trivially reachable by any connected peer; requires only knowledge of valid block hashes.

---

### Recommendation

Add an IBD guard at the top of `GetBlocksProcess::execute()`, mirroring the pattern in `GetHeadersProcess`:

```rust
pub fn execute(self) -> Status {
    let active_chain = self.synchronizer.shared.active_chain();

    if active_chain.is_initial_block_download() {
        // send InIBD and return early, consistent with GetHeadersProcess
        self.send_in_ibd();
        return Status::ignored();
    }

    // ... existing block-serving logic ...
}
```

The `send_in_ibd` helper should be extracted to a shared utility or duplicated from `GetHeadersProcess` so both handlers respond consistently.

---

### Proof of Concept

1. Start a CKB node from genesis (it will be in IBD — `is_initial_block_download()` returns `true`).
2. Connect a custom P2P peer that sends a `SyncMessage::GetBlocks` containing the genesis block hash (publicly known).
3. Observe that the node **does not** respond with `InIBD` (as `GetHeadersProcess` would) and instead looks up and serves the genesis block.
4. Repeat in a tight loop with a list of known early block hashes to continuously consume the IBD node's bandwidth and CPU.

The discrepancy is confirmed by the module comment: [6](#0-5) 

and the absence of any `is_initial_block_download()` call in: [3](#0-2)

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

**File:** sync/src/synchronizer/get_headers_process.rs (L101-115)
```rust
    fn send_in_ibd(&self) {
        let content = packed::InIBD::new_builder().build();
        let message = packed::SyncMessage::new_builder().set(content).build();
        let nc = Arc::clone(self.nc);
        let peer = self.peer;
        self.synchronizer
            .shared()
            .shared()
            .async_handle()
            .spawn(async move {
                let _ignore =
                    async_send_message(SupportProtocols::Sync.protocol_id(), &nc, peer, &message)
                        .await;
            });
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
