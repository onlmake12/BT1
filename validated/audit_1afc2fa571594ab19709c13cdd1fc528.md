Audit Report

## Title
Missing Per-Peer Rate Limit on `LightClientProtocol` Enables Unbounded MMR Database Reads — (`util/light-client-protocol-server/src/lib.rs`)

## Summary
`LightClientProtocol` carries no `governor`-based rate limiter, unlike `Relayer` and `HolePunching` which both enforce 30 req/s per peer. Every `GetLastState` message unconditionally triggers `get_verifiable_tip_header()`, which performs a RocksDB snapshot acquisition, a block-body read, and O(log N) MMR reads from `COLUMN_CHAIN_ROOT_MMR`. An unprivileged peer can flood this handler at TCP line rate with no throttle, producing sustained unbounded I/O and CPU amplification on the full node.

## Finding Description
`LightClientProtocol` is defined with only a `shared` field and no rate limiter: [1](#0-0) 

`try_process` dispatches directly to handlers with zero rate-limit checks: [2](#0-1) 

`GetLastStateProcess::execute()` calls `get_verifiable_tip_header()` unconditionally — the `subscribe` flag only sets a peer flag, it does not gate the expensive work: [3](#0-2) 

`get_verifiable_tip_header()` performs: (1) `self.shared.snapshot()` — Arc clone of chain snapshot, (2) `snapshot.get_block(&tip_hash)` — RocksDB read from `COLUMN_BLOCK_BODY`, (3) `snapshot.chain_root_mmr(tip_block.number() - 1).get_root()` — O(log N) RocksDB reads from `COLUMN_CHAIN_ROOT_MMR`: [4](#0-3) 

By contrast, `Relayer` carries an explicit `rate_limiter: RateLimiter<(PeerIndex, u32)>` field and checks it before any handler dispatch: [5](#0-4) [6](#0-5) 

`HolePunching` applies the same pattern: [7](#0-6) [8](#0-7) 

A `grep` for `rate_limiter` under `util/light-client-protocol-server/` returns zero matches, confirming no guard exists anywhere in the light-client protocol server.

## Impact Explanation
A single unprivileged peer on the `LightClient` protocol can send `GetLastState` at maximum TCP rate. Each message triggers one block-body RocksDB read and O(log N) MMR reads (where N is mainnet chain height — millions of blocks). Sustained flooding produces unbounded CPU and disk I/O amplification with no per-peer throttle. Multiple attacking peers multiply the effect linearly. This matches the allowed CKB bounty impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion / crash a CKB node with few costs** (10001–15000 points). The cost to the attacker is negligible (a TCP connection and a tight send loop); the cost to the victim scales with chain height and attacker count.

## Likelihood Explanation
Any peer that can open a connection on the `LightClient` protocol endpoint can trigger this. No authentication, no PoW, no privileged role, and no special knowledge is required. The `subscribe` flag value is irrelevant — the expensive MMR read occurs on every `GetLastState` message regardless. The attack is trivially repeatable and requires only a standard TCP client.

## Recommendation
Add a `governor`-based `RateLimiter<(PeerIndex, u32)>` field to `LightClientProtocol` (mirroring `Relayer::rate_limiter` at `sync/src/relayer/mod.rs:81`) and check it at the top of `try_process` before dispatching to any handler, using the same 30 req/s per-peer-per-message-type quota already established in the codebase. Call `rate_limiter.retain_recent()` in the `disconnected` handler to bound memory growth.

## Proof of Concept
1. Connect a peer to the node's `LightClient` protocol endpoint (no credentials required).
2. In a tight loop, send `GetLastState { subscribe: false }` (or `true` — the work is identical).
3. Observe via RocksDB statistics or `perf` that `COLUMN_CHAIN_ROOT_MMR` read IOPS and CPU time scale linearly with message send rate.
4. Confirm that no `TooManyRequests` status is ever returned (unlike `Relayer`, which returns it after 30 req/s per peer per message type).
5. Repeat with N concurrent attacking peers and observe linear scaling of node resource consumption.

### Citations

**File:** util/light-client-protocol-server/src/lib.rs (L26-29)
```rust
pub struct LightClientProtocol {
    /// Sync shared state.
    pub shared: Shared,
}
```

**File:** util/light-client-protocol-server/src/lib.rs (L96-107)
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

**File:** sync/src/relayer/mod.rs (L78-82)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L38-47)
```rust
pub(crate) struct HolePunching {
    network_state: Arc<NetworkState>,
    bind_addr: Option<SocketAddr>,
    // Request timestamp recorded
    inflight_requests: HashMap<PeerId, u64>,
    // Delivered timestamp recorded
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
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
