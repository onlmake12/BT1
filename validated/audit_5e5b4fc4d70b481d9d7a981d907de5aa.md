Let me check the MMR store implementation and whether there's any network-layer backpressure.

The evidence is conclusive. Here is the full analysis:

---

### Title
Missing Per-Peer Rate Limiting on `GetLastState` Allows Unbounded MMR Root Computation and Send-Queue Exhaustion — (`util/light-client-protocol-server/src/lib.rs`, `util/light-client-protocol-server/src/components/get_last_state.rs`)

---

### Summary

`LightClientProtocol` has no rate limiter on any message type. A single unprivileged peer can flood the server with `GetLastState{subscribe:false}` messages at wire speed. Every message unconditionally triggers a snapshot acquisition, an O(log N) RocksDB read sequence (`chain_root_mmr(tip-1).get_root()`), response serialization, and an async network send. No ban, throttle, or back-pressure is applied for well-formed messages. This is a concrete design gap relative to every other comparable CKB protocol handler.

---

### Finding Description

**Entrypoint:** Any peer connected on the `LightClient` protocol sub-stream can send a well-formed `GetLastState` molecule message.

**Dispatch path:**

`LightClientProtocol::received` (lib.rs:55–92) parses the message and immediately calls `self.try_process(...)` with no rate-limit check. [1](#0-0) 

`try_process` dispatches to `GetLastStateProcess::execute` (lib.rs:103–107). [2](#0-1) 

`GetLastStateProcess::execute` unconditionally calls `self.protocol.get_verifiable_tip_header()` on every message (get_last_state.rs:40–45). [3](#0-2) 

`get_verifiable_tip_header` calls `snapshot.chain_root_mmr(tip_block.number() - 1).get_root()` — an O(log N) RocksDB read sequence over `COLUMN_CHAIN_ROOT_MMR` (lib.rs:137–144). [4](#0-3) 

The MMR store backend for `&Snapshot` reads each node from RocksDB via `get_header_digest` (snapshot/src/lib.rs:293–296). [5](#0-4) 

After computing the root, a `SendLastState` response is serialized and enqueued via `async_send_message` (prelude.rs:21–23). [6](#0-5) 

**No guard exists.** The `LightClientProtocol` struct holds only `shared: Shared` — no `rate_limiter` field. [7](#0-6) 

A grep for `rate_limiter`, `RateLimiter`, or `rate_limit` across the entire `util/light-client-protocol-server/` tree returns zero matches.

**Contrast with peer protocols:**

`Relayer` carries a `rate_limiter: RateLimiter<(PeerIndex, u32)>` capped at 30 req/s per peer per message type, checked before any processing. [8](#0-7) [9](#0-8) 

`HolePunching` carries the same 30 req/s per-peer rate limiter, checked at the top of `received`. [10](#0-9) 

`LightClientProtocol` is the only production protocol handler with no such guard.

---

### Impact Explanation

For a mainnet chain at ~13 M blocks, `get_root()` reads ≈ 23 MMR peak nodes from RocksDB per call. Under a message flood:

1. **CPU/IO saturation:** Repeated RocksDB reads (even with block-cache hits) compete with block verification and sync I/O on the same thread pool.
2. **Send-queue exhaustion:** Each message generates a `SendLastState` response. The async send queue fills; legitimate peers (sync, relay) experience head-of-line blocking or dropped messages.
3. **No self-limiting:** `subscribe:false` sets no peer state, so the handler is entirely stateless and repeatable. A successful response always returns `Status::ok()`, which never triggers a ban. [11](#0-10) 

---

### Likelihood Explanation

- Requires only a valid TCP connection to a node with the light client protocol enabled.
- The message is a minimal molecule struct (a single boolean field); trivial to generate at high rate.
- No PoW, no stake, no fee, no prior authentication.
- The attacker is never banned for sending valid messages.

---

### Recommendation

Add a per-peer, per-message-type rate limiter to `LightClientProtocol`, mirroring the pattern already used in `Relayer` and `HolePunching`:

1. Add a `rate_limiter: RateLimiter<(PeerIndex, u32)>` field to `LightClientProtocol`.
2. In `LightClientProtocol::received` (or at the top of `try_process`), call `self.rate_limiter.check_key(&(peer, msg.item_id()))` and return `StatusCode::TooManyRequests` on failure — without banning.
3. A quota of 10–30 req/s per peer per message type is consistent with the existing protocol budgets.

Additionally, consider caching the `(tip_hash → VerifiableHeader)` result so that repeated requests for the same tip do not re-execute the MMR root computation.

---

### Proof of Concept

```rust
// Pseudocode — mirrors the existing test harness in
// util/light-client-protocol-server/src/tests/

let chain = MockChain::new();
chain.mine_to(1000); // N blocks → ~10 MMR reads per GetLastState

let nc = MockNetworkContext::new(SupportProtocols::LightClient);
let mut protocol = chain.create_light_client_protocol();
let peer = PeerIndex::new(1);

// Craft a minimal GetLastState{subscribe:false}
let msg = packed::LightClientMessage::new_builder()
    .set(packed::GetLastState::new_builder().subscribe(false.pack()).build())
    .build()
    .as_bytes();

let M: usize = 10_000;
let start = std::time::Instant::now();
for _ in 0..M {
    protocol.received(nc.context(), peer, msg.clone()).await;
}
let elapsed = start.elapsed();

// Assert: M responses were sent, peer was never banned, no throttle applied
assert_eq!(nc.sent_messages().borrow().len(), M);
assert!(nc.not_banned(peer));
// Wall-clock time will be proportional to M × (MMR reads + send overhead)
// with no rate-limited baseline to compare against
println!("{M} GetLastState messages processed in {elapsed:?}");
```

### Citations

**File:** util/light-client-protocol-server/src/lib.rs (L26-29)
```rust
pub struct LightClientProtocol {
    /// Sync shared state.
    pub shared: Shared,
}
```

**File:** util/light-client-protocol-server/src/lib.rs (L55-92)
```rust
    async fn received(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        data: Bytes,
    ) {
        trace!("LightClient.received peer={}", peer);

        let msg = match packed::LightClientMessageReader::from_slice(&data) {
            Ok(msg) => msg.to_enum(),
            _ => {
                warn!(
                    "LightClient.received a malformed message from Peer({})",
                    peer
                );
                nc.ban_peer(
                    peer,
                    constant::BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };

        let item_name = msg.item_name();
        let status = self.try_process(&nc, peer, msg).await;
        if let Some(ban_time) = status.should_ban() {
            error!(
                "process {} from {}; ban {:?} since result is {}",
                item_name, peer, ban_time, status
            );
            nc.ban_peer(peer, ban_time, status.to_string());
        } else if status.should_warn() {
            warn!("process {} from {}; result is {}", item_name, peer, status);
        } else if !status.is_ok() {
            debug!("process {} from {}; result is {}", item_name, peer, status);
        }
    }
```

**File:** util/light-client-protocol-server/src/lib.rs (L102-107)
```rust
        match message {
            packed::LightClientMessageUnionReader::GetLastState(reader) => {
                components::GetLastStateProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
```

**File:** util/light-client-protocol-server/src/lib.rs (L134-145)
```rust
        let parent_chain_root = if tip_block.is_genesis() {
            Default::default()
        } else {
            let mmr = snapshot.chain_root_mmr(tip_block.number() - 1);
            match mmr.get_root() {
                Ok(root) => root,
                Err(err) => {
                    let errmsg = format!("failed to generate a root since {err:?}");
                    return Err(errmsg);
                }
            }
        };
```

**File:** util/light-client-protocol-server/src/components/get_last_state.rs (L29-55)
```rust
    pub(crate) async fn execute(self) -> Status {
        let subscribe: bool = self.message.subscribe().into();
        if subscribe {
            self.nc.with_peer_mut(
                self.peer,
                Box::new(|peer| {
                    peer.if_lightclient_subscribed = true;
                }),
            );
        }

        let tip_header = match self.protocol.get_verifiable_tip_header() {
            Ok(tip_state) => tip_state,
            Err(errmsg) => {
                return StatusCode::InternalError.with_context(errmsg);
            }
        };

        let content = packed::SendLastState::new_builder()
            .last_header(tip_header)
            .build();
        let message = packed::LightClientMessage::new_builder()
            .set(content)
            .build();

        self.nc.reply(self.peer, &message).await
    }
```

**File:** util/snapshot/src/lib.rs (L293-296)
```rust
impl MMRStore<HeaderDigest> for &Snapshot {
    fn get_elem(&self, pos: u64) -> MMRResult<Option<HeaderDigest>> {
        Ok(self.store.get_header_digest(pos))
    }
```

**File:** util/light-client-protocol-server/src/prelude.rs (L21-23)
```rust
        if let Err(err) = self
            .async_send_message(protocol_id, peer_index, message.as_bytes())
            .await
```

**File:** sync/src/relayer/mod.rs (L81-98)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L95-107)
```rust
        if self
            .rate_limiter
            .check_key(&(session_id, msg.item_id()))
            .is_err()
        {
            debug!(
                "process {} from {}; result is {}",
                item_name,
                session_id,
                status::StatusCode::TooManyRequests.with_context(msg.item_name())
            );
            return;
        }
```
