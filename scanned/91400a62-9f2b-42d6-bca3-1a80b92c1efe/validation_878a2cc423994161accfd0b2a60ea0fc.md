The investigation is complete. Here is the assessment:

---

### Title
Missing Per-Peer Rate Limiting on Light Client Protocol Messages Enables Server Resource Exhaustion — (`util/light-client-protocol-server/src/lib.rs`)

### Summary
`LightClientProtocol` contains no rate limiter. Every `GetLastState` message from any peer unconditionally triggers a snapshot acquisition, two DB reads, an MMR object construction, and an MMR root computation. Other production protocols (`Relayer`, `HolePunching`) both carry a `governor`-based `RateLimiter<(PeerIndex, u32)>` and enforce a 30 req/sec per-peer-per-message-type cap before dispatching. The light client protocol has no equivalent guard.

### Finding Description

`LightClientProtocol` is defined with only a single field: [1](#0-0) 

No rate limiter field exists anywhere in the struct or its `impl` blocks. The `received` handler parses the message and immediately calls `try_process`: [2](#0-1) 

`try_process` dispatches directly to the four handlers with no rate check: [3](#0-2) 

`GetLastStateProcess::execute()` calls `get_verifiable_tip_header()` on every invocation: [4](#0-3) 

`get_verifiable_tip_header()` performs: snapshot acquisition → `tip_hash()` DB read → `get_block()` DB read → `chain_root_mmr(tip_number - 1)` construction → `mmr.get_root()` DB read: [5](#0-4) 

By contrast, `Relayer::try_process` checks a `governor` rate limiter keyed by `(PeerIndex, message_item_id)` at 30 req/sec before any handler is invoked: [6](#0-5) 

`HolePunching::received` does the same check before dispatching: [7](#0-6) 

A grep for `rate_limiter` across the entire `util/light-client-protocol-server/` tree returns zero matches, confirming the guard is entirely absent.

### Impact Explanation
A single unprivileged peer maintaining one persistent TCP connection can send `GetLastState` at wire speed. Each message forces the server to: acquire a shared snapshot, perform two RocksDB point reads, construct an MMR over the full chain height, and compute the MMR root (which itself reads O(log N) DB nodes for a chain of height N). There is no counter, token bucket, or cooldown to bound this per peer. The result is unbounded CPU and DB I/O consumption attributable to one peer, degrading or blocking service for all other peers and the node's own chain-processing tasks.

### Likelihood Explanation
The attack requires only a valid P2P connection and the ability to send well-formed `GetLastState` messages in a loop — no PoW, no keys, no special privileges. The `GetLastState` message body is minimal (a single boolean `subscribe` field), so bandwidth cost to the attacker is negligible. The path is directly reachable from any peer on the public network.

### Recommendation
Add a `governor::RateLimiter<(PeerIndex, u32)>` field to `LightClientProtocol`, initialize it in `LightClientProtocol::new` with the same 30 req/sec quota used by `Relayer` and `HolePunching`, and check it at the top of `try_process` before dispatching to any handler — mirroring the pattern already established in `sync/src/relayer/mod.rs` lines 116–123.

### Proof of Concept
1. Connect a peer to a CKB node with the light client protocol enabled.
2. In a tight loop, send `LightClientMessage { GetLastState { subscribe: false } }` at maximum network speed.
3. Monitor server-side RocksDB read IOPS and CPU usage; both will scale linearly with message rate from that single peer with no upper bound enforced by the server.
4. Assert that resource consumption is not bounded by any per-peer limit (it will not be, as confirmed by the absence of any rate limiter in the code path).

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

**File:** util/light-client-protocol-server/src/lib.rs (L127-155)
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

        let tip_header = packed::VerifiableHeader::new_builder()
            .header(tip_block.header().data())
            .uncles_hash(tip_block.calc_uncles_hash())
            .extension(Pack::pack(&tip_block.extension()))
            .parent_chain_root(parent_chain_root)
            .build();

        Ok(tip_header)
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
