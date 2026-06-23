### Title
Missing Rate Limiting in `BlockFilter` Handler Enables Single-Peer Starvation of All Filter Protocol Peers — (`sync/src/filter/mod.rs`, `sync/src/filter/get_block_filter_hashes_process.rs`)

---

### Summary

The `BlockFilter` protocol handler processes all messages from all peers sequentially in a single `ServiceProtocol` task, with no per-peer rate limiting. A single unprivileged peer can flood `GetBlockFilterHashes` messages, each triggering up to 2000 synchronous DB lookups in a tight loop with no async yield points, starving all other peers' `GetBlockFilters` and `GetBlockFilterCheckPoints` requests indefinitely.

---

### Finding Description

**Architecture — single sequential handler for all peers**

`CKBProtocol::build()` registers the `BlockFilter` handler as `ProtocolHandle::Callback` via `service_handle()`: [1](#0-0) 

This makes `CKBHandler` a tentacle `ServiceProtocol` — a single handler instance that receives events from **all** sessions. In `CKBHandler::received`, the call is a direct `.await` on `&mut self`: [2](#0-1) 

Because `&mut self` is held across the `.await`, no other message from any peer can be processed until the current one completes. This is the fundamental serialization point.

**`BlockFilter::received` — no rate limiting, direct sequential dispatch** [3](#0-2) 

There is no rate limiter check before `self.process(nc, peer_index, msg).await`. Compare with `Relayer`, which has an explicit `governor::RateLimiter` keyed by `(peer, message_type)` at 30 req/s: [4](#0-3) 

`BlockFilter` has no equivalent. A grep for `rate_limiter` in `sync/src/**` returns only `relayer/mod.rs` — zero hits in the filter module.

**`GetBlockFilterHashesProcess::execute()` — tight synchronous loop, no yield** [5](#0-4) 

The loop at lines 53–66 iterates up to `BATCH_SIZE = 2000` times, calling `active_chain.get_block_hash()` and `active_chain.get_block_filter_hash()` — both synchronous RocksDB reads — with **no `.await` yield point** inside the loop. The only `.await` is `async_send_message_to` at line 76, after the loop completes. This means the async executor thread is held for the entire duration of 2000 DB reads per message.

`GetBlockFiltersProcess` has the same structure with `BATCH_SIZE = 1000`: [6](#0-5) 

---

### Impact Explanation

An attacker connects as a peer and sends a continuous flood of `GetBlockFilterHashes` messages. Each message occupies the single `ServiceProtocol` task for the duration of 2000 synchronous DB reads. Because all peers share this one sequential handler, legitimate light clients (peer B, C, …) sending `GetBlockFilters` or `GetBlockFilterCheckPoints` have their messages queued behind the attacker's flood. Light client filter sync is effectively denied for all honest peers for as long as the attacker maintains the flood. The cost to the attacker is minimal: a single TCP connection and a stream of small, valid messages.

---

### Likelihood Explanation

The attack requires only a standard P2P connection — no privileged role, no PoW, no key material. The Filter protocol is open to any peer that negotiates it. The absence of rate limiting (present in `Relayer` and `HolePunching` but absent in `BlockFilter`) is a concrete, exploitable design gap, not a theoretical one. The starvation is deterministic given the sequential handler model.

---

### Recommendation

1. Add a `governor::RateLimiter` keyed by `(PeerIndex, message_type)` to `BlockFilter`, mirroring the pattern in `Relayer` (30 req/s cap with `retain_recent()` on disconnect).
2. Insert a `tokio::task::yield_now().await` inside the `for` loops in `GetBlockFilterHashesProcess::execute()` and `GetBlockFiltersProcess::execute()` to allow cooperative multitasking between iterations.
3. Consider offloading the DB-read loop to a blocking thread pool via `nc.async_future_task(..., blocking: true)` so it does not occupy the async executor.

---

### Proof of Concept

```
1. Run a CKB node with Filter protocol enabled.
2. Connect peer A: open a tight loop sending GetBlockFilterHashes{start_number: 0}
   at maximum TCP throughput.
3. Connect peer B: send GetBlockFilters{start_number: 0} and measure response latency.
4. Observe: peer B's response is delayed by the entire backlog of peer A's messages,
   each requiring up to 2000 synchronous RocksDB reads before the handler yields.
5. Disconnect peer A: peer B's latency immediately returns to baseline.
```

The latency difference between step 4 and step 5 directly demonstrates the starvation. No special privileges, no PoW, no key material required.

### Citations

**File:** network/src/protocols/mod.rs (L290-296)
```rust
            .service_handle(move || {
                ProtocolHandle::Callback(Box::new(CKBHandler {
                    proto_id: self.id,
                    network_state: Arc::clone(&self.network_state),
                    handler: self.handler,
                }))
            })
```

**File:** network/src/protocols/mod.rs (L365-384)
```rust
    async fn received(&mut self, context: ProtocolContextMutRef<'_>, data: Bytes) {
        if !self.network_state.is_active() {
            return;
        }

        trace!(
            "[received message]: {}, {}, length={}",
            self.proto_id,
            context.session.id,
            data.len()
        );
        let nc = DefaultCKBProtocolContext {
            proto_id: self.proto_id,
            network_state: Arc::clone(&self.network_state),
            p2p_control: context.control().to_owned().into(),
            async_p2p_control: context.control().to_owned(),
        };
        let peer_index = context.session.id;
        self.handler.received(Arc::new(nc), peer_index, data).await;
    }
```

**File:** sync/src/filter/mod.rs (L122-160)
```rust
    async fn received(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
        data: Bytes,
    ) {
        let msg = match packed::BlockFilterMessageReader::from_compatible_slice(&data) {
            Ok(msg) => msg.to_enum(),
            _ => {
                info_target!(
                    crate::LOG_TARGET_FILTER,
                    "Peer {} sends us a malformed message",
                    peer_index
                );
                nc.ban_peer(
                    peer_index,
                    BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };

        debug_target!(
            crate::LOG_TARGET_FILTER,
            "received msg {} from {}",
            msg.item_name(),
            peer_index
        );
        let start_time = Instant::now();
        self.process(nc, peer_index, msg).await;
        debug_target!(
            crate::LOG_TARGET_FILTER,
            "process message={}, peer={}, cost={:?}",
            msg.item_name(),
            peer_index,
            Instant::now().saturating_duration_since(start_time),
        );
    }
```

**File:** sync/src/relayer/mod.rs (L84-123)
```rust
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

    /// Get shared state
    pub fn shared(&self) -> &Arc<SyncShared> {
        &self.shared
    }

    async fn try_process(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        message: packed::RelayMessageUnionReader<'_>,
    ) -> Status {
        // CompactBlock will be verified by POW, it's OK to skip rate limit checking.
        let should_check_rate =
            !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));

        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L32-80)
```rust
    pub async fn execute(self) -> Status {
        let active_chain = self.filter.shared.active_chain();
        let start_number: BlockNumber = self.message.to_entity().start_number().into();
        let latest: BlockNumber = active_chain.get_latest_built_filter_block_number();

        let mut block_filter_hashes = Vec::new();

        if latest >= start_number {
            let parent_block_filter_hash = if start_number > 0 {
                match active_chain
                    .get_block_hash(start_number - 1)
                    .and_then(|block_hash| active_chain.get_block_filter_hash(&block_hash))
                {
                    Some(parent_block_filter_hash) => parent_block_filter_hash,
                    None => return Status::ignored(),
                }
            } else {
                packed::Byte32::zero()
            };

            let mut block_number = start_number;
            for _ in 0..BATCH_SIZE {
                if let Some(block_filter_hash) = active_chain
                    .get_block_hash(block_number)
                    .and_then(|block_hash| active_chain.get_block_filter_hash(&block_hash))
                {
                    block_filter_hashes.push(block_filter_hash);
                } else {
                    break;
                }
                let Some(next_block_number) = block_number.checked_add(1) else {
                    break;
                };
                block_number = next_block_number;
            }
            let content = packed::BlockFilterHashes::new_builder()
                .start_number(start_number)
                .parent_block_filter_hash(parent_block_filter_hash)
                .block_filter_hashes(block_filter_hashes)
                .build();

            let message = packed::BlockFilterMessage::new_builder()
                .set(content)
                .build();
            async_send_message_to(&self.nc, self.peer, &message).await
        } else {
            Status::ignored()
        }
    }
```

**File:** sync/src/filter/get_block_filters_process.rs (L33-85)
```rust
    pub async fn execute(self) -> Status {
        let active_chain = self.filter.shared.active_chain();
        let start_number: BlockNumber = self.message.to_entity().start_number().into();
        let latest: BlockNumber = active_chain.get_latest_built_filter_block_number();

        if latest >= start_number {
            let mut block_hashes = Vec::new();
            let mut filters = Vec::new();
            let mut current_content_size = 0;
            current_content_size += 8; // Size of start_number
            current_content_size += 4 * 2; // Size of the header field `full-size` of `block_hash` and `block_filter`
            let mut block_number = start_number;
            for _ in 0..BATCH_SIZE {
                if let Some(block_hash) = active_chain.get_block_hash(block_number) {
                    if let Some(block_filter) = active_chain.get_block_filter(&block_hash) {
                        if current_content_size
                            + block_hash.as_slice().len()
                            + 4
                            + block_filter.as_slice().len()
                            + 4
                            >= (1.8 * 1024.0 * 1024.0) as usize
                        {
                            // Break if the encoded size of `block_hash` + `block_filter` + `start_number` + molecule header increase reaches 1.8MB, to avoid frame size too large
                            break;
                        }
                        current_content_size +=
                            block_hash.as_slice().len() + block_filter.as_slice().len() + 4;
                        block_hashes.push(block_hash);
                        filters.push(block_filter);
                    } else {
                        break;
                    }
                } else {
                    break;
                }
                let Some(next_block_number) = block_number.checked_add(1) else {
                    break;
                };
                block_number = next_block_number;
            }
            let content = packed::BlockFilters::new_builder()
                .start_number(start_number)
                .block_hashes(block_hashes)
                .filters(filters)
                .build();
            let message = packed::BlockFilterMessage::new_builder()
                .set(content)
                .build();
            async_send_message_to(&self.nc, self.peer, &message).await
        } else {
            Status::ignored()
        }
    }
```
