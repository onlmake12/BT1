### Title
Missing Per-Peer Rate Limit on `GetLastState` Handler Enables Handler Monopolization — (`util/light-client-protocol-server/src/components/get_last_state.rs`)

---

### Summary

`LightClientProtocol` has no rate limiter for any message type. A single connected peer can flood `GetLastState` messages, each triggering O(log N) RocksDB reads via `chain_root_mmr` + `get_root()`, monopolizing the sequential async handler and starving all other peers' light-client messages.

---

### Finding Description

`LightClientProtocol` is the sole handler for all light-client P2P messages. Its struct contains only a `shared: Shared` field — no rate limiter exists. [1](#0-0) 

The `received` method takes `&mut self`, making it sequential: every incoming message from every peer is processed one at a time through the same handler. There is no rate check before dispatching: [2](#0-1) 

For `GetLastState`, `execute()` unconditionally calls `get_verifiable_tip_header()`: [3](#0-2) 

`get_verifiable_tip_header()` acquires a snapshot, then calls `snapshot.chain_root_mmr(tip_block.number() - 1)` followed by `mmr.get_root()`: [4](#0-3) 

`chain_root_mmr` constructs an MMR backed by the RocksDB snapshot. `get_root()` performs O(log N) database reads where N is the current chain height (on mainnet, ~10M blocks → ~23 reads per call): [5](#0-4) 

**Contrast with protocols that do have rate limiting:**

- `Relayer` has a `governor`-based `RateLimiter<(PeerIndex, u32)>` capped at 30 req/sec per peer per message type, checked before any processing: [6](#0-5) 

- `HolePunching` has both a `rate_limiter` and a `forward_rate_limiter`, checked at the top of `received`: [7](#0-6) 

`LightClientProtocol` has neither.

---

### Impact Explanation

A single connected peer sending a tight loop of `GetLastState` messages monopolizes the `&mut self` async handler. Because the handler is sequential, no other peer's light-client messages (including `GetLastStateProof`, `GetBlocksProof`, `GetTransactionsProof`) can be processed while the flood is in progress. Each message causes O(log N) RocksDB reads. At mainnet chain height this is ~23 reads per message; at 10,000 messages/second that is ~230,000 DB reads/second sustained from a single peer, degrading I/O for the entire node process. The light-client handler becomes effectively unresponsive to all other peers for the duration of the attack.

---

### Likelihood Explanation

Any peer that successfully connects on the `LightClient` protocol can immediately begin flooding. No PoW, no stake, no privileged role is required. The `GetLastState` message is the smallest and cheapest message in the protocol (a single boolean field `subscribe`), making it trivially spammable. The absence of a rate limiter — despite the pattern being established in `Relayer` and `HolePunching` — makes this straightforwardly exploitable.

---

### Recommendation

Add a `governor`-based `RateLimiter<(PeerIndex, u32)>` to `LightClientProtocol`, mirroring the pattern in `Relayer`. Check it at the top of `received` before dispatching to `try_process`, and return early (or ban the peer after repeated violations) when the limit is exceeded. A cap of 10–30 `GetLastState` requests per second per peer is sufficient for any legitimate light-client use case.

---

### Proof of Concept

```
1. Connect a peer to the target node on the LightClient protocol.
2. In a tight loop, send GetLastState { subscribe: false } messages.
3. Observe: the LightClient handler processes each message sequentially,
   performing ~23 RocksDB reads per message.
4. Observe: other peers' GetLastStateProof / GetBlocksProof messages
   are queued and not processed until the flood stops.
5. Measure: CPU and I/O utilization on the serving node rises proportionally
   to the message rate; handler latency for legitimate peers grows unboundedly.
``` [3](#0-2) [1](#0-0)

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

**File:** util/snapshot/src/lib.rs (L180-184)
```rust
    /// Returns the chain root MMR for a provided block.
    pub fn chain_root_mmr(&self, block_number: BlockNumber) -> ChainRootMMR<&Self> {
        let mmr_size = leaf_index_to_mmr_size(block_number);
        ChainRootMMR::new(mmr_size, self)
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
