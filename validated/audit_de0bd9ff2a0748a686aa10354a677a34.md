### Title
Missing Per-Peer Rate Limit on Filter Protocol Enables DB Read Amplification DoS — (`sync/src/filter/mod.rs`)

---

### Summary

The `BlockFilter` P2P protocol handler has no rate limiter of any kind. An unprivileged remote peer can flood all three `Get*` message types at maximum speed, each triggering hundreds to thousands of synchronous RocksDB reads per message, with no throttling to bound the aggregate read load.

---

### Finding Description

The `BlockFilter::received` handler dispatches directly to `try_process` with zero rate-limit checks: [1](#0-0) 

Compare this to the `Relayer` protocol, which explicitly constructs a `RateLimiter` keyed by `(peer, message.item_id())` at 30 req/s and checks it before every dispatch: [2](#0-1) 

A `grep` for `rate_limiter` across all of `sync/src/` confirms it appears **only** in `sync/src/relayer/mod.rs` — the `BlockFilter` struct and its handler contain no such field or check.

Each message type performs a tight DB-read loop with a hard upper bound on iterations but no time or rate bound:

- **`GetBlockFilterCheckPoints`**: `BATCH_SIZE = 2000`, each iteration calls `get_block_hash` + `get_block_filter_hash` → up to **4 000 DB reads** per message. [3](#0-2) 

- **`GetBlockFilterHashes`**: `BATCH_SIZE = 2000`, same two-read pattern → up to **4 001 DB reads** per message. [4](#0-3) 

- **`GetBlockFilters`**: `BATCH_SIZE = 1000`, calls `get_block_hash` + `get_block_filter` → up to **2 000 DB reads** per message (plus filter blob I/O). [5](#0-4) 

A single peer sending one of each message type back-to-back produces ~10 001 DB reads per round-trip, with no mechanism to stop repetition.

---

### Impact Explanation

Sustained flooding saturates the shared RocksDB read thread pool and block-cache bandwidth. This degrades or blocks all concurrent DB operations — chain sync, block validation, tx-pool lookups — causing effective node unresponsiveness (DoS). A hard process crash is unlikely from reads alone, but the node can become unable to process any other work, which is operationally equivalent for a production validator or full node.

---

### Likelihood Explanation

The attack requires only a standard P2P connection — no authentication, no PoW, no stake. The Filter protocol is enabled by default on nodes that support light-client serving. The attacker controls `start_number` freely; setting it to a valid low block number guarantees the full batch is always served. Multiple peers amplify the effect linearly.

---

### Recommendation

Add a per-peer, per-message-type rate limiter to `BlockFilter`, mirroring the pattern already used in `Relayer`:

- Introduce a `RateLimiter<(PeerIndex, u32)>` field in the `BlockFilter` struct.
- In `received`, check `rate_limiter.check_key(&(peer_index, msg.item_id()))` before calling `process`, returning early (and optionally banning) on `Err`.
- Tune the quota conservatively (e.g., 1–5 req/s per peer per message type) given the high per-message DB cost.

---

### Proof of Concept

```
1. Connect to a CKB node with the Filter protocol enabled.
2. In a tight loop, send:
     GetBlockFilterCheckPoints { start_number: 0 }
     GetBlockFilterHashes      { start_number: 0 }
     GetBlockFilters           { start_number: 0 }
3. Repeat at network speed from a single TCP connection.
4. Monitor RocksDB read latency and the node's ability to process
   new blocks or relay transactions — both degrade rapidly.
5. No ban or disconnect is triggered because no status code
   indicating abuse is ever returned by the filter handlers.
```

### Citations

**File:** sync/src/filter/mod.rs (L122-152)
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

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L9-56)
```rust
const BATCH_SIZE: BlockNumber = 2000;
const CHECK_POINT_INTERVAL: BlockNumber = 2000;

pub struct GetBlockFilterCheckPointsProcess<'a> {
    message: packed::GetBlockFilterCheckPointsReader<'a>,
    filter: &'a BlockFilter,
    nc: Arc<dyn CKBProtocolContext + Sync>,
    peer: PeerIndex,
}

impl<'a> GetBlockFilterCheckPointsProcess<'a> {
    pub fn new(
        message: packed::GetBlockFilterCheckPointsReader<'a>,
        filter: &'a BlockFilter,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
    ) -> Self {
        Self {
            message,
            nc,
            filter,
            peer,
        }
    }

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

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L8-66)
```rust
const BATCH_SIZE: BlockNumber = 2000;

pub struct GetBlockFilterHashesProcess<'a> {
    message: packed::GetBlockFilterHashesReader<'a>,
    filter: &'a BlockFilter,
    nc: Arc<dyn CKBProtocolContext + Sync>,
    peer: PeerIndex,
}

impl<'a> GetBlockFilterHashesProcess<'a> {
    pub fn new(
        message: packed::GetBlockFilterHashesReader<'a>,
        filter: &'a BlockFilter,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
    ) -> Self {
        Self {
            message,
            nc,
            filter,
            peer,
        }
    }

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
```

**File:** sync/src/filter/get_block_filters_process.rs (L9-71)
```rust
const BATCH_SIZE: BlockNumber = 1000;

pub struct GetBlockFiltersProcess<'a> {
    message: packed::GetBlockFiltersReader<'a>,
    filter: &'a BlockFilter,
    nc: Arc<dyn CKBProtocolContext + Sync>,
    peer: PeerIndex,
}

impl<'a> GetBlockFiltersProcess<'a> {
    pub fn new(
        message: packed::GetBlockFiltersReader<'a>,
        filter: &'a BlockFilter,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
    ) -> Self {
        Self {
            message,
            nc,
            filter,
            peer,
        }
    }

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
```
