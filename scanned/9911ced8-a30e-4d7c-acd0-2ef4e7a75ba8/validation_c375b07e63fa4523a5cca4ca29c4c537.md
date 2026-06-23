Based on my investigation, here is the complete analysis:

---

### Title
Unauthenticated Peer Can Flood `GetBlockFilterCheckPoints` Without Rate Limiting, Triggering Up to 4000 RocksDB Reads Per Message — (`sync/src/filter/get_block_filter_check_points_process.rs`)

### Summary

The `BlockFilter` protocol handler has no rate limiter, unlike the `Relayer` which explicitly guards every message type with a `governor::RateLimiter`. An unprivileged peer can flood `GetBlockFilterCheckPoints` messages with `start_number=0`, causing `execute()` to perform up to 2000 loop iterations × 2 DB reads = 4000 RocksDB reads per message, with no throttle.

### Finding Description

**Confirmed: No rate limiter in `BlockFilter`**

The `BlockFilter` struct contains only `shared: Arc<SyncShared>`: [1](#0-0) 

The `received()` handler directly calls `self.process()` with zero rate-limit checks: [2](#0-1) 

**Contrast with `Relayer`**, which has an explicit `rate_limiter: RateLimiter<(PeerIndex, u32)>` field and checks it before dispatching every message: [3](#0-2) [4](#0-3) 

**Confirmed: 4000 RocksDB reads per message**

`execute()` loops up to `BATCH_SIZE=2000` times, each iteration calling `get_block_hash()` then `get_block_filter_hash()`: [5](#0-4) [6](#0-5) 

With `start_number=0` and `CHECK_POINT_INTERVAL=2000`, the loop reads blocks at heights 0, 2000, 4000, …, 3,998,000 — all 2000 checkpoints — on a 4M-block chain.

**Confirmed: Filter protocol is enabled by default**

`default_support_all_protocols()` includes `SupportProtocol::Filter`: [7](#0-6) 

The default `ckb.toml` also lists `"Filter"` in `support_protocols`: [8](#0-7) 

The Filter handler is registered unconditionally when the protocol is in the support list: [9](#0-8) 

### Impact Explanation

A single unauthenticated peer connecting to any default-configured CKB full node can:

1. Send a continuous stream of `GetBlockFilterCheckPoints` messages with `start_number=0`.
2. Each message forces 4000 RocksDB point-lookups (2000 × `get_block_hash` + 2000 × `get_block_filter_hash`).
3. Because the handler is `&mut self` and processes messages sequentially, the async message queue grows unboundedly under flood conditions, consuming memory.
4. Sustained I/O pressure degrades the node's ability to serve legitimate sync/relay peers, and unbounded queue growth can lead to OOM termination.

The `GetBlockFilterHashes` handler has the same pattern and the same gap: [10](#0-9) [11](#0-10) 

### Likelihood Explanation

- No authentication or PoW required; any peer can connect.
- Filter is on by default in mainnet/testnet config.
- The message is tiny (a single `uint64` field), so the attacker's bandwidth cost is negligible.
- The asymmetry (tiny request → 4000 DB reads) is the classic amplification pattern for resource exhaustion.

### Recommendation

Add a `governor::RateLimiter<(PeerIndex, u32)>` to `BlockFilter` mirroring the existing `Relayer` pattern, and check it at the top of `try_process()` before dispatching `GetBlockFilterCheckPoints` and `GetBlockFilterHashes`.

### Proof of Concept

1. Sync a CKB node to mainnet height ≥ 4,000,000 with default config (Filter enabled).
2. Open a raw P2P connection and negotiate the `/ckb/filter` protocol.
3. In a tight loop, send `BlockFilterMessage { GetBlockFilterCheckPoints { start_number: 0 } }`.
4. Monitor RocksDB read IOPS (`iostat`) and the node's async task queue depth.
5. Assert: IOPS saturates and the node becomes unresponsive to other peers' Sync/Relay messages within seconds.

### Citations

**File:** sync/src/filter/mod.rs (L21-25)
```rust
#[derive(Clone)]
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
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

**File:** sync/src/relayer/mod.rs (L113-123)
```rust
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

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L9-10)
```rust
const BATCH_SIZE: BlockNumber = 2000;
const CHECK_POINT_INTERVAL: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L41-56)
```rust
        if latest >= start_number {
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
                let Some(next_block_number) = block_number.checked_add(CHECK_POINT_INTERVAL) else {
                    break;
                };
                block_number = next_block_number;
            }
```

**File:** util/app-config/src/configs/network.rs (L236-250)
```rust
pub fn default_support_all_protocols() -> Vec<SupportProtocol> {
    vec![
        SupportProtocol::Ping,
        SupportProtocol::Discovery,
        SupportProtocol::Identify,
        SupportProtocol::Feeler,
        SupportProtocol::DisconnectMessage,
        SupportProtocol::Sync,
        SupportProtocol::Relay,
        SupportProtocol::Time,
        SupportProtocol::Alert,
        SupportProtocol::LightClient,
        SupportProtocol::Filter,
        SupportProtocol::HolePunching,
    ]
```

**File:** resource/ckb.toml (L111-112)
```text
# Supported protocols list, only "Sync" and "Identify" are mandatory, others are optional
support_protocols = ["Ping", "Discovery", "Identify", "Feeler", "DisconnectMessage", "Sync", "Relay", "Time", "Alert", "LightClient", "Filter", "HolePunching"]
```

**File:** util/launcher/src/lib.rs (L443-456)
```rust
        if support_protocols.contains(&SupportProtocol::Filter) {
            let filter = BlockFilter::new(Arc::clone(&sync_shared));

            protocols.push(
                CKBProtocol::new_with_support_protocol(
                    SupportProtocols::Filter,
                    Box::new(filter),
                    Arc::clone(&network_state),
                )
                .compress(false),
            );
        } else {
            flags.remove(Flags::BLOCK_FILTER);
        }
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L8-8)
```rust
const BATCH_SIZE: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L52-66)
```rust
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
```
