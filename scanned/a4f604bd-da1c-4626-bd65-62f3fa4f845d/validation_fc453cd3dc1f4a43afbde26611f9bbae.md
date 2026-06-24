The code confirms all claims in the report. All cited code paths are accurate and the asymmetry is real.

Audit Report

## Title
Missing Per-Peer Rate Limit on `LightClientProtocol` Allows Unbounded MMR RocksDB Reads — (`util/light-client-protocol-server/src/lib.rs`)

## Summary
`LightClientProtocol` has no rate-limiting field or guard in `try_process`. Every incoming `GetLastState` message unconditionally triggers `get_verifiable_tip_header()`, which performs a live RocksDB MMR root computation (`chain_root_mmr(tip_number - 1).get_root()`). A single unprivileged peer can flood this path at maximum socket speed, driving unbounded RocksDB I/O on the shared store while the `Relayer` protocol applies an equivalent 30 req/s cap per `(peer, message_type)`.

## Finding Description
`LightClientProtocol` is a plain struct with only a `shared: Shared` field and no rate limiter: [1](#0-0) 

`try_process` dispatches directly to handlers with zero rate-limiting logic: [2](#0-1) 

Every `GetLastState` message unconditionally calls `get_verifiable_tip_header()` regardless of the `subscribe` flag: [3](#0-2) 

`get_verifiable_tip_header()` always performs a live RocksDB MMR root computation: [4](#0-3) 

By contrast, `Relayer::try_process` checks a `governor`-based `RateLimiter<(PeerIndex, u32)>` at 30 req/s before dispatching any non-PoW message: [5](#0-4) 

`LightClientProtocol` has no equivalent guard, leaving the MMR read path fully exposed.

## Impact Explanation
This matches **High: bad designs which could cause CKB network congestion with few costs**. A single peer can sustain thousands of `GetLastState` messages per second over one TCP connection. Each message forces O(log N) RocksDB reads on the shared snapshot store used by block verification. Sustained flooding degrades block-processing throughput and P2P responsiveness proportionally to message rate, with no mechanism to throttle or ban the offending peer (the messages are well-formed and return `Status::ok()`).

## Likelihood Explanation
The attack requires only a standard P2P connection — no privileges, no PoW, no key material. `GetLastState` carries a single boolean field, making it the smallest possible message in the protocol. The node's `max_inbound` cap (default 125) bounds the number of peers but not per-peer message rate. The attack is trivially repeatable and requires no victim mistakes.

## Recommendation
Add a `governor`-based `RateLimiter<(PeerIndex, u32)>` field to `LightClientProtocol`, initialize it in `new()` at 30 req/s per `(peer, message_type)` matching the `Relayer` pattern, and check it at the top of `try_process` before dispatching to any handler. Optionally call `rate_limiter.retain_recent()` in `disconnected()` to bound memory growth, mirroring `Relayer::disconnected`. [6](#0-5) 

## Proof of Concept
1. Start a CKB full node with `LightClient` in `support_protocols`.
2. Connect a peer and send `GetLastState { subscribe: false }` in a tight loop at maximum socket speed.
3. Observe RocksDB read IOPS spike (via `rocksdb.stats`) and block-processing latency increase (via `ckb_chain_process_block_duration` metrics).
4. Compare against a peer sending `GetRelayTransactions` at the same rate — the `Relayer`'s 30 req/s cap returns `StatusCode::TooManyRequests` and stops processing; the light-client handler processes every message unconditionally. [7](#0-6)

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

**File:** util/light-client-protocol-server/src/lib.rs (L96-125)
```rust
    async fn try_process(
        &mut self,
        nc: &Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
        message: packed::LightClientMessageUnionReader<'_>,
    ) -> Status {
        match message {
            packed::LightClientMessageUnionReader::GetLastState(reader) => {
                components::GetLastStateProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            packed::LightClientMessageUnionReader::GetLastStateProof(reader) => {
                components::GetLastStateProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            packed::LightClientMessageUnionReader::GetBlocksProof(reader) => {
                components::GetBlocksProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            packed::LightClientMessageUnionReader::GetTransactionsProof(reader) => {
                components::GetTransactionsProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            _ => StatusCode::UnexpectedProtocolMessage.into(),
        }
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

**File:** util/light-client-protocol-server/src/components/get_last_state.rs (L40-45)
```rust
        let tip_header = match self.protocol.get_verifiable_tip_header() {
            Ok(tip_state) => tip_state,
            Err(errmsg) => {
                return StatusCode::InternalError.with_context(errmsg);
            }
        };
```

**File:** sync/src/relayer/mod.rs (L63-67)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;
```

**File:** sync/src/relayer/mod.rs (L89-123)
```rust
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
