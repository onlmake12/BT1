Based on my code investigation, here is my assessment:

---

### Title
Unbounded RocksDB Read Amplification via Unauthenticated `GetBlockFilterCheckPoints` Flood — (`sync/src/filter/get_block_filter_check_points_process.rs`)

### Summary
The `BlockFilter` protocol handler has no rate limiter. A single unprivileged peer can send tiny (~9-byte) `GetBlockFilterCheckPoints` messages in a tight loop, each triggering up to 2000 iterations of two RocksDB reads, causing sustained I/O exhaustion at negligible attacker cost.

### Finding Description

The `Relayer` handler explicitly carries a `governor::RateLimiter<(PeerIndex, u32), ...>` capped at 30 req/s per `(peer, message_type)` pair: [1](#0-0) [2](#0-1) [3](#0-2) 

The `BlockFilter` handler has no such field or check anywhere in its struct or `received`/`process` path: [4](#0-3) [5](#0-4) 

`GetBlockFilterCheckPointsProcess::execute` loops up to `BATCH_SIZE = 2000` times, performing two RocksDB reads per iteration (`get_block_hash` via snapshot + `get_block_filter_hash` via store): [6](#0-5) [7](#0-6) 

The loop only exits early if `latest < start_number`. With `start_number = 0` and any synced node (`latest >= 0`), all 2000 iterations execute, reading block hashes at heights 0, 2000, 4000, …, 3,998,000 — up to 4,000 RocksDB point-reads per message.

No ban or disconnect is issued for valid repeated requests; the only ban path is for malformed messages: [8](#0-7) 

### Impact Explanation

Each ~9-byte attacker message causes up to 4,000 RocksDB reads (two per checkpoint interval across 2,000 steps). A single persistent connection sending at loopback speed can saturate the node's storage I/O and async task queue. Because the Filter protocol handler processes messages sequentially (`.await` on each `process` call), the handler goroutine is fully occupied servicing the flood, starving legitimate peers.

### Likelihood Explanation

The Filter protocol is a production feature registered in the launcher. Any peer that negotiates the Filter protocol (no authentication required) can exploit this. The attacker needs only one TCP connection and a loop sending the same 9-byte message. The contrast with `Relayer`'s explicit rate limiter confirms the developers are aware of the pattern but omitted it here.

### Recommendation

Add a per-peer rate limiter to `BlockFilter` mirroring the `Relayer` pattern:
- Add a `governor::RateLimiter<(PeerIndex, u32), ...>` field to `BlockFilter`.
- In `try_process`, check the rate before dispatching to any `*Process::execute`.
- Additionally, consider capping `BATCH_SIZE` responses or adding a per-peer request counter with a sliding window.

### Proof of Concept

```
1. Connect to a CKB node with the Filter protocol.
2. In a tight loop, send:
     GetBlockFilterCheckPoints { start_number: 0 }
   (encoded: ~9 bytes)
3. Observe: each message triggers up to 2000 × 2 = 4000 RocksDB reads.
4. Measure RocksDB read rate and async task queue depth.
5. Assert: without a rate limiter, the filter handler is fully saturated
   while the attacker sends only ~9-byte messages.
```

The call chain is:
`BlockFilter::received` → `BlockFilter::process` → `BlockFilter::try_process` → `GetBlockFilterCheckPointsProcess::execute` → loop 0..2000 { `get_block_hash` + `get_block_filter_hash` } [9](#0-8)

### Citations

**File:** sync/src/relayer/mod.rs (L63-67)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;
```

**File:** sync/src/relayer/mod.rs (L81-92)
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

**File:** sync/src/filter/mod.rs (L21-25)
```rust
#[derive(Clone)]
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
}
```

**File:** sync/src/filter/mod.rs (L88-114)
```rust
        if let Some(ban_time) = status.should_ban() {
            error_target!(
                crate::LOG_TARGET_RELAY,
                "receive {} from {}, ban {:?} for {}",
                item_name,
                peer,
                ban_time,
                status
            );
            nc.ban_peer(peer, ban_time, status.to_string());
        } else if status.should_warn() {
            warn_target!(
                crate::LOG_TARGET_RELAY,
                "receive {} from {}, {}",
                item_name,
                peer,
                status
            );
        } else if !status.is_ok() {
            debug_target!(
                crate::LOG_TARGET_RELAY,
                "receive {} from {}, {}",
                item_name,
                peer,
                status
            );
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

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L9-10)
```rust
const BATCH_SIZE: BlockNumber = 2000;
const CHECK_POINT_INTERVAL: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L34-56)
```rust
    pub async fn execute(self) -> Status {
        let active_chain = self.filter.shared.active_chain();
        let start_number: BlockNumber = self.message.to_entity().start_number().into();
        let latest: BlockNumber = active_chain.get_latest_built_filter_block_number();

        let mut block_filter_hashes = Vec::new();

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
