### Title
Missing Per-Peer Rate Limiting on Synchronizer Protocol Message Handler Allows Sustained Resource Exhaustion — (File: `sync/src/synchronizer/mod.rs`)

### Summary

The `Synchronizer` P2P protocol handler processes `GetHeaders` and `GetBlocks` messages from any connected peer with no per-peer rate limiting. Unlike the `Relayer`, which explicitly enforces a 30 req/sec rate limit per `(PeerIndex, message_type)` pair, the `Synchronizer` has no equivalent guard. A single malicious inbound peer can flood the node with `GetHeaders` messages, each triggering up to 101 DB hash lookups and a response of up to 2,000 serialized block headers, causing sustained CPU and disk I/O exhaustion.

### Finding Description

**Root cause — missing rate limiter in `Synchronizer`:**

The `Relayer` struct explicitly carries a `rate_limiter` field and checks it at the top of `try_process` before dispatching any message:

```rust
// sync/src/relayer/mod.rs
pub struct Relayer {
    chain: ChainController,
    pub(crate) shared: Arc<SyncShared>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,  // ← present
}
``` [1](#0-0) 

The rate check fires before any message is processed:

```rust
if should_check_rate
    && self.rate_limiter.check_key(&(peer, message.item_id())).is_err()
{
    return StatusCode::TooManyRequests.with_context(message.item_name());
}
``` [2](#0-1) 

The `Synchronizer` struct has **no such field and no such check**:

```rust
pub struct Synchronizer {
    pub(crate) chain: ChainController,
    pub shared: Arc<SyncShared>,
    fetch_channel: Option<channel::Sender<FetchCMD>>,
    // ← no rate_limiter
}
``` [3](#0-2) 

`try_process` dispatches directly to handlers with zero throttling: [4](#0-3) 

**`GetHeaders` handler cost per message:**

`GetHeadersProcess::execute` accepts up to `MAX_LOCATOR_SIZE` (101) hashes, calls `locate_latest_common_block` (DB lookups), then calls `get_locator_response` which reads up to `MAX_HEADERS_LEN` (2,000) headers from RocksDB and serializes them into a `SendHeaders` response: [5](#0-4) [6](#0-5) 

`MAX_LOCATOR_SIZE` and `MAX_HEADERS_LEN` are defined as: [7](#0-6) [8](#0-7) 

**`GetBlocks` handler cost per message:**

`GetBlocksProcess::execute` accepts up to `MAX_HEADERS_LEN` (2,000) block hashes, reads each full block from DB, and spawns async tasks to send them: [9](#0-8) 

### Impact Explanation

A single connected malicious peer can send `GetHeaders` or `GetBlocks` messages at line rate. Each `GetHeaders` message causes:
- Up to 101 RocksDB hash lookups (locator resolution)
- Up to 2,000 RocksDB header reads
- Serialization and async dispatch of a large `SendHeaders` response

This creates sustained disk I/O, CPU, and network pressure on the victim node. At sufficient message rate, the node's ability to process legitimate blocks, relay transactions, and serve honest peers degrades. The attacker needs only a single inbound TCP connection — no special privileges, no stake, no PoW.

The contrast with the `Relayer` (which caps at 30 msg/sec per peer per message type) confirms the developers recognize this threat model for P2P handlers; the `Synchronizer` was simply not given the same protection.

### Likelihood Explanation

CKB nodes accept inbound connections from any peer by default (`max_peers = 125`). An attacker establishes one connection and sends `GetHeaders` in a tight loop. No authentication, no fee, no PoW is required. The only existing defense is the `BAD_MESSAGE_BAN_TIME` (5 minutes) ban applied on malformed messages — but well-formed `GetHeaders` messages with valid (even random) locator hashes are not malformed and will not trigger a ban. [10](#0-9) 

### Recommendation

Add a `RateLimiter<(PeerIndex, u32)>` field to `Synchronizer` (mirroring the `Relayer` pattern) and check it at the top of `try_process` before dispatching `GetHeaders` and `GetBlocks`. A quota of 30 req/sec per `(peer, message_type)` — consistent with the Relayer — would be a reasonable starting point. Peers that exceed the limit should receive a `TooManyRequests` status and, after repeated violations, be banned.

### Proof of Concept

1. Connect a custom P2P client to a CKB full node using the `Sync` protocol.
2. In a tight loop, send `SyncMessage::GetHeaders` with a locator of 101 arbitrary 32-byte hashes and `hash_stop = Byte32::zero()`.
3. Observe: the node performs DB lookups and sends `SendHeaders` responses for every message with no delay or rejection.
4. Monitor node CPU and disk I/O — both climb proportionally to the send rate.
5. Compare: sending the same volume of `RelayMessage::GetRelayTransactions` via the `Relay` protocol triggers `TooManyRequests` after 30 messages/second and the peer is throttled.

The asymmetry between the two handlers — identical threat model, one protected, one not — is the root cause.

### Citations

**File:** sync/src/relayer/mod.rs (L77-82)
```rust
/// Relayer protocol handle
pub struct Relayer {
    chain: ChainController,
    pub(crate) shared: Arc<SyncShared>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}
```

**File:** sync/src/relayer/mod.rs (L116-123)
```rust
        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```

**File:** sync/src/synchronizer/mod.rs (L357-362)
```rust
pub struct Synchronizer {
    pub(crate) chain: ChainController,
    /// Sync shared state
    pub shared: Arc<SyncShared>,
    fetch_channel: Option<channel::Sender<FetchCMD>>,
}
```

**File:** sync/src/synchronizer/mod.rs (L381-423)
```rust
    async fn try_process(
        &self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        message: packed::SyncMessageUnionReader<'_>,
    ) -> Status {
        let _trace_timecost: Option<HistogramTimer> = {
            ckb_metrics::handle().map(|handle| {
                handle
                    .ckb_sync_msg_process_duration
                    .with_label_values(&[message.item_name()])
                    .start_timer()
            })
        };

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
    }
```

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

**File:** sync/src/types/mod.rs (L1905-1921)
```rust
    pub fn get_locator_response(
        &self,
        block_number: BlockNumber,
        hash_stop: &Byte32,
    ) -> Vec<core::HeaderView> {
        let tip_number = self.tip_header().number();
        let Some(start_number) = block_number.checked_add(1) else {
            return Vec::new();
        };
        std::iter::successors(Some(start_number), |number| number.checked_add(1))
            .take_while(|number| *number <= tip_number)
            .take(MAX_HEADERS_LEN)
            .filter_map(|block_number| self.snapshot.get_block_hash(block_number))
            .take_while(|block_hash| block_hash != hash_stop)
            .filter_map(|block_hash| self.sync_shared.store().get_block_header(&block_hash))
            .collect()
    }
```

**File:** util/constant/src/sync.rs (L7-8)
```rust
/// Default max get header response length, if it is greater than this value, the message will be ignored
pub const MAX_HEADERS_LEN: usize = 2_000;
```

**File:** util/constant/src/sync.rs (L44-45)
```rust
/// The maximum number of entries in a locator
pub const MAX_LOCATOR_SIZE: usize = 101;
```

**File:** util/constant/src/sync.rs (L59-62)
```rust
/// Default ban time for message
// ban time
// 5 minutes
pub const BAD_MESSAGE_BAN_TIME: Duration = Duration::from_secs(5 * 60);
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
