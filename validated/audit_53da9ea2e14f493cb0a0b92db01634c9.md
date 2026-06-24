Audit Report

## Title
Missing Per-Peer Rate Limit on `LightClientProtocol` Enables Handler Monopolization — (`util/light-client-protocol-server/src/lib.rs`)

## Summary
`LightClientProtocol` contains no rate limiter of any kind. A single connected peer can flood `GetLastState` messages, each of which unconditionally triggers O(log N) RocksDB reads via `chain_root_mmr` + `get_root()`. Because the tentacle framework dispatches all messages for a given protocol through a single `&mut self` handler instance sequentially, this monopolizes the light-client handler and starves all other peers' light-client requests for the duration of the flood.

## Finding Description
`LightClientProtocol` holds only a `shared: Shared` field — no rate limiter exists. [1](#0-0) 

The `CKBHandler` wrapper (which wraps `LightClientProtocol` as a `ServiceProtocol`) is registered as `ProtocolHandle::Callback`, meaning a single handler instance is shared across all sessions. Its `received` method directly awaits `self.handler.received(...)`, making all message processing sequential per protocol. [2](#0-1) 

There is no rate check anywhere in `received` before dispatching to `try_process`: [3](#0-2) 

For `GetLastState`, `execute()` unconditionally calls `get_verifiable_tip_header()`, which acquires a snapshot and performs `snapshot.chain_root_mmr(tip_block.number() - 1)` followed by `mmr.get_root()` — O(log N) RocksDB reads at current mainnet height (~23 reads per call): [4](#0-3) [5](#0-4) 

By contrast, `Relayer` has a `governor`-based `RateLimiter<(PeerIndex, u32)>` capped at 30 req/sec per peer per message type, checked before any processing: [6](#0-5) 

`HolePunching` similarly checks `rate_limiter` and `forward_rate_limiter` at the top of `received`: [7](#0-6) 

`LightClientProtocol` has neither guard. The `GetLastState` message is the smallest in the protocol (a single boolean field `subscribe`), making it trivially spammable at high rate. [8](#0-7) 

## Impact Explanation
A single connected peer sending a tight loop of `GetLastState { subscribe: false }` messages monopolizes the light-client handler. Because the handler is sequential, no other peer's light-client messages (`GetLastStateProof`, `GetBlocksProof`, `GetTransactionsProof`) can be processed while the flood is in progress. Each message causes ~23 RocksDB reads at mainnet height, generating sustained I/O load on the node. The light-client service becomes effectively unresponsive to all legitimate light-client peers for the duration of the attack. This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs** — the light-client portion of the CKB network is rendered non-functional for all peers at negligible attacker cost (sending minimal-size messages).

## Likelihood Explanation
Any peer that successfully connects on the `LightClient` protocol can immediately begin flooding. No PoW, stake, or privileged role is required. The `GetLastState` message is the cheapest message in the protocol. The absence of a rate limiter — despite the pattern being established in `Relayer` and `HolePunching` — makes this straightforwardly exploitable by any network participant.

## Recommendation
Add a `governor`-based `RateLimiter<(PeerIndex, u32)>` field to `LightClientProtocol`, mirroring the pattern in `Relayer`. Check it at the top of `received` before dispatching to `try_process`, returning early (or banning the peer after repeated violations) when the limit is exceeded. A cap of 10–30 `GetLastState` requests per second per peer is sufficient for any legitimate light-client use case. Call `rate_limiter.retain_recent()` in `disconnected` to avoid unbounded memory growth.

## Proof of Concept
```
1. Connect a peer to the target node on the LightClient protocol (/ckb/lightclient).
2. In a tight loop, send GetLastState { subscribe: false } messages
   (molecule-encoded, ~10 bytes each, well within the 2MB frame limit).
3. Observe: the LightClientProtocol handler processes each message sequentially,
   performing ~23 RocksDB reads per message via chain_root_mmr + get_root().
4. Simultaneously, have a second legitimate peer send GetBlocksProof or
   GetTransactionsProof requests.
5. Observe: the legitimate peer's requests are queued and not processed until
   the flood from peer 1 stops, because the single &mut self handler is occupied.
6. Measure: RocksDB read I/O on the serving node rises proportionally to the
   flood rate; handler latency for legitimate peers grows unboundedly.
```

### Citations

**File:** util/light-client-protocol-server/src/lib.rs (L25-29)
```rust
/// Light client protocol handler.
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

**File:** util/light-client-protocol-server/src/lib.rs (L127-145)
```rust
    pub(crate) fn get_verifiable_tip_header(&self) -> Result<packed::VerifiableHeader, String> {
        let snapshot = self.shared.snapshot();

        let tip_hash = snapshot.tip_hash();
        let tip_block = snapshot
            .get_block(&tip_hash)
            .expect("checked: tip block should be existed");
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

**File:** sync/src/relayer/mod.rs (L81-123)
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

**File:** network/src/protocols/support_protocols.rs (L133-133)
```rust
            SupportProtocols::LightClient => 2 * 1024 * 1024, // 2 MB
```
