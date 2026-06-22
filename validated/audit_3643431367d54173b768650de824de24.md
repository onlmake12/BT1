Based on my investigation of the code, here is my assessment:

---

### Title
Missing Per-Peer Rate Limit on `GetLastState` Allows Unbounded MMR RocksDB Reads — (`util/light-client-protocol-server/src/lib.rs`)

### Summary

`LightClientProtocol::try_process` dispatches every incoming `GetLastState` message directly to `GetLastStateProcess::execute` with no rate-limiting guard. Each execution unconditionally calls `get_verifiable_tip_header()`, which performs `chain_root_mmr(tip_number - 1).get_root()` — a live RocksDB read of O(log N) MMR nodes. A single unprivileged peer can send these messages at maximum socket speed, driving unbounded RocksDB I/O that contends with block processing on the shared store.

### Finding Description

`LightClientProtocol` is defined as a plain struct with only a `shared: Shared` field — no rate limiter: [1](#0-0) 

`try_process` dispatches directly to handlers with zero rate-limiting logic: [2](#0-1) 

Every `GetLastState` message — regardless of the `subscribe` flag — unconditionally calls `get_verifiable_tip_header()`: [3](#0-2) 

`get_verifiable_tip_header()` always performs a live RocksDB MMR root computation: [4](#0-3) 

`chain_root_mmr` constructs a `ChainRootMMR` sized to `leaf_index_to_mmr_size(block_number)`, and `get_root()` reads O(log N) nodes from the shared RocksDB snapshot: [5](#0-4) 

### Impact Explanation

The `Relayer` explicitly guards every non-PoW message with a `governor`-based rate limiter keyed by `(peer, message_type)` at 30 req/s: [6](#0-5) 

`HolePunching` applies the same pattern: [7](#0-6) 

`LightClientProtocol` has no equivalent guard. A single peer can saturate the RocksDB read path shared with block verification (`chain/src/verify.rs` uses the same MMR store), degrading block-processing throughput and P2P responsiveness proportionally to message rate.

### Likelihood Explanation

The attack requires only a standard P2P connection — no privileges, no PoW, no key material. `GetLastState` is a tiny fixed-size message (one boolean field), so a single TCP connection can sustain thousands of messages per second. The node's `max_inbound` cap (default 125) does not bound per-peer message rate. [8](#0-7) 

### Recommendation

Add a `governor`-based `RateLimiter<(PeerIndex, u32)>` field to `LightClientProtocol`, initialize it in `new()` (e.g., 30 req/s per `(peer, message_type)` matching the Relayer), and check it at the top of `try_process` before dispatching to any handler — mirroring the pattern in `Relayer::try_process`.

### Proof of Concept

1. Start a CKB full node with `LightClient` in `support_protocols`.
2. Connect a peer and send `GetLastState { subscribe: false }` in a tight loop at maximum socket speed.
3. Observe RocksDB read IOPS spike (via `rocksdb.stats`) and block-processing latency increase (via `ckb_chain_process_block_duration` metrics).
4. Compare against a peer sending `GetRelayTransactions` at the same rate — the Relayer's 30 req/s cap will throttle it; the light-client handler will not. [9](#0-8)

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

**File:** util/light-client-protocol-server/src/components/get_last_state.rs (L29-45)
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
```

**File:** util/snapshot/src/lib.rs (L181-184)
```rust
    pub fn chain_root_mmr(&self, block_number: BlockNumber) -> ChainRootMMR<&Self> {
        let mmr_size = leaf_index_to_mmr_size(block_number);
        ChainRootMMR::new(mmr_size, self)
    }
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

**File:** util/light-client-protocol-server/src/constant.rs (L1-7)
```rust
use std::time::Duration;

pub const BAD_MESSAGE_BAN_TIME: Duration = Duration::from_secs(5 * 60);

pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```
