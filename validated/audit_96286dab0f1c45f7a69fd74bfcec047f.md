### Title
Missing IBD Guard in `GetBlocksProcess` Allows Peers to Force Block Serving During Initial Block Download — (`sync/src/synchronizer/get_blocks_process.rs`)

---

### Summary

The CKB synchronizer explicitly documents that both `GetHeaders` and `GetBlocks` messages must be rejected with an `InIBD` response when the node is in Initial Block Download (IBD) mode. The guard is correctly applied to `GetHeadersProcess`, but is entirely absent from `GetBlocksProcess`. Any unprivileged inbound peer can exploit this to force an IBD node to look up, serialize, and transmit full blocks from local storage, consuming CPU, I/O, and bandwidth while the node is already resource-constrained during sync.

---

### Finding Description

The module-level documentation in `sync/src/synchronizer/mod.rs` states the design contract explicitly:

> "When CKB node is in IBD mode, it will respond `packed::InIBD` to `GetHeaders` and `GetBlocks` requests" [1](#0-0) 

`GetHeadersProcess::execute()` correctly enforces this contract. When `is_initial_block_download()` returns `true`, it sends an `InIBD` response and returns early: [2](#0-1) 

`GetBlocksProcess::execute()` has **no such check**. It proceeds directly to look up and serve blocks from local storage regardless of IBD state: [3](#0-2) 

The dispatcher in `try_process` routes `GetBlocks` messages directly to `GetBlocksProcess` with no IBD pre-check: [4](#0-3) 

The IBD state check itself is well-defined and reliable — once the node exits IBD it never re-enters: [5](#0-4) 

---

### Impact Explanation

During IBD, a node is maximally resource-constrained: it is downloading, deserializing, and verifying a large chain of blocks. An attacker who connects as an inbound peer can send repeated `GetBlocks` messages containing up to `MAX_HEADERS_LEN` (2000) block hashes per message. For each valid hash, the node performs a RocksDB lookup, deserializes the block, and sends it back over the network. This:

1. Consumes disk I/O competing with the IBD block-write path.
2. Consumes CPU for serialization.
3. Consumes outbound bandwidth, which may be limited.
4. Slows the IBD process, extending the window during which the node is vulnerable and not fully validating the chain.

The attacker needs only publicly known block hashes (available from any block explorer or other node) and a single TCP connection.

---

### Likelihood Explanation

CKB nodes accept inbound connections during IBD. The `GetBlocks` message is a standard sync protocol message. No authentication, stake, or privilege is required. The block hashes needed to trigger real work are public. The attack is trivially scriptable: connect, send a stream of `GetBlocks` messages with known hashes, repeat. The rate limiter in `Relayer` does not apply here — this is the `Synchronizer` protocol, which has no per-message rate limiting. [6](#0-5) 

---

### Recommendation

Add an IBD guard to `GetBlocksProcess::execute()` mirroring the pattern in `GetHeadersProcess`:

```rust
pub fn execute(self) -> Status {
    let active_chain = self.synchronizer.shared.active_chain();

+   if active_chain.is_initial_block_download() {
+       // Optionally send InIBD response here
+       return Status::ignored();
+   }

    let block_hashes = self.message.block_hashes();
    // ... rest of existing logic
}
```

This matches the documented design intent and closes the asymmetry between the two handlers.

---

### Proof of Concept

1. Start a CKB node from genesis (IBD state, `is_initial_block_download()` returns `true`).
2. Connect as an inbound peer on the Sync protocol.
3. Send a `GetBlocks` message containing 2000 known block hashes (e.g., genesis hash repeated or hashes from a public chain snapshot).
4. Observe: the node performs up to 2000 RocksDB lookups and sends block data back, despite being in IBD mode.
5. Repeat in a tight loop. The `GetHeadersProcess` would have returned `InIBD` and ignored the request; `GetBlocksProcess` does not.

The missing guard is confirmed by the absence of any `is_initial_block_download()` call in `sync/src/synchronizer/get_blocks_process.rs`: [3](#0-2) 

compared to the present guard in `sync/src/synchronizer/get_headers_process.rs`: [2](#0-1)

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

**File:** sync/src/relayer/mod.rs (L63-99)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;

#[derive(Debug, Eq, PartialEq)]
pub enum ReconstructionResult {
    Block(BlockView),
    Missing(Vec<usize>, Vec<usize>),
    Collided,
    Error(Status),
}

/// Relayer protocol handle
pub struct Relayer {
    chain: ChainController,
    pub(crate) shared: Arc<SyncShared>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}

impl Relayer {
    /// Init relay protocol handle
    ///
    /// This is a runtime relay protocol shared state, and any relay messages will be processed and forwarded by it
    pub fn new(chain: ChainController, shared: Arc<SyncShared>) -> Self {
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        Relayer {
            chain,
            shared,
            rate_limiter,
        }
    }
```
