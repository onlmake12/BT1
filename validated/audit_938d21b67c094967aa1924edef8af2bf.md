### Title
Unbounded RocksDB I/O Amplification via Unauthenticated `GetBlockFilterHashes` Flood — (`sync/src/filter/get_block_filter_hashes_process.rs`, `sync/src/filter/mod.rs`)

---

### Summary

`BlockFilter`, the handler for `SupportProtocols::Filter`, contains **no per-peer rate limiter**. A single well-formed 9-byte `GetBlockFilterHashes` message with `start_number=0` causes up to **4,000 RocksDB reads** (2,000 × `get_block_hash` + 2,000 × `get_block_filter_hash`). Because the Filter protocol is enabled by default and no request throttling exists, any unprivileged remote peer can drive unbounded DB I/O amplification. With up to ~117 concurrent inbound peers each flooding at line rate, this exhausts the node's I/O and async task queue, severely degrading or crashing the node.

---

### Finding Description

**Entrypoint:** Any peer that opens `SupportProtocols::Filter` (protocol id 121, enabled by default) can send a `GetBlockFilterHashes` message.

**Call chain:**

```
BlockFilter::received          (sync/src/filter/mod.rs:152)
  → BlockFilter::process       (sync/src/filter/mod.rs:78)
    → BlockFilter::try_process (sync/src/filter/mod.rs:45-48)
      → GetBlockFilterHashesProcess::execute
```

Inside `execute`, the loop runs up to `BATCH_SIZE = 2000` iterations, each performing two RocksDB reads:

```rust
for _ in 0..BATCH_SIZE {                          // up to 2000 iterations
    if let Some(block_filter_hash) = active_chain
        .get_block_hash(block_number)             // RocksDB read #1
        .and_then(|h| active_chain.get_block_filter_hash(&h)) // RocksDB read #2
    { ... }
}
``` [1](#0-0) [2](#0-1) 

**Missing guard — no rate limiter in `BlockFilter`:**

The `BlockFilter` struct holds only `shared: Arc<SyncShared>` — no `rate_limiter` field exists. [3](#0-2) 

`BlockFilter::received` calls `self.process(...)` directly with zero throttling: [4](#0-3) 

**Contrast with `Relayer`**, which has an explicit `governor`-based per-peer rate limiter (30 req/s keyed by `(PeerIndex, message_type)`) checked before any dispatch: [5](#0-4) [6](#0-5) 

**Filter is enabled by default** in `default_support_all_protocols()`: [7](#0-6) 

**Peer count bound:** `max_peers = 125`, `max_outbound_peers = 8`, so up to ~117 inbound peers can connect simultaneously. [8](#0-7) 

The same missing rate limit applies identically to `GetBlockFilterCheckPoints` (also `BATCH_SIZE=2000`) and `GetBlockFilters` (`BATCH_SIZE=1000`): [9](#0-8) [10](#0-9) 

---

### Impact Explanation

Each 9-byte `GetBlockFilterHashes(start_number=0)` message triggers up to 4,000 synchronous RocksDB reads. With 117 inbound peers each sending at maximum rate, the victim node's RocksDB I/O and async task queue saturate, starving the Sync and Relay protocols of processing time. This causes the node to become unresponsive to block/transaction relay, effectively removing it from the network. Since Filter is on by default, all full nodes are equally exposed.

---

### Likelihood Explanation

The attack requires only:
1. Opening a TCP connection to a CKB full node (no authentication, no PoW, no stake).
2. Sending a valid molecule-encoded `GetBlockFilterHashes` message (9 bytes).
3. Repeating at line rate from multiple IPs.

No privileged access, no key material, no majority hashpower is needed. The exploit is locally testable and requires no special tooling beyond a basic P2P client.

---

### Recommendation

Add a `governor`-based per-peer rate limiter to `BlockFilter`, mirroring the pattern already used in `Relayer`:

```rust
pub struct BlockFilter {
    shared: Arc<SyncShared>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,  // add this
}
```

In `try_process`, check the limiter before dispatching any `GetBlock*` message variant, returning `StatusCode::TooManyRequests` on excess. A quota of 1–2 requests/second per peer per message type is sufficient for legitimate light-client use.

---

### Proof of Concept

```python
# Pseudocode — connect N peers, flood GetBlockFilterHashes(start_number=0)
import socket, struct

# Molecule-encoded GetBlockFilterHashes { start_number: 0 }
# Header (4 bytes full_size=9) + union tag (4 bytes=0) + start_number (8 bytes LE = 0)
MSG = bytes.fromhex("09000000" + "00000000" + "0000000000000000")

peers = [connect_ckb_filter_protocol("victim_ip", 8115) for _ in range(117)]
while True:
    for p in peers:
        p.send(MSG)  # 9 bytes → triggers 4000 RocksDB reads on victim
```

Measure victim RocksDB read IOPS and async task queue depth; assert node stops responding to Sync/Relay messages within seconds.

### Citations

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L8-8)
```rust
const BATCH_SIZE: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L53-66)
```rust
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

**File:** sync/src/filter/mod.rs (L22-25)
```rust
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
}
```

**File:** sync/src/filter/mod.rs (L151-152)
```rust
        let start_time = Instant::now();
        self.process(nc, peer_index, msg).await;
```

**File:** sync/src/relayer/mod.rs (L78-98)
```rust
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

**File:** resource/ckb.toml (L94-95)
```text
max_peers = 125
max_outbound_peers = 8
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L9-9)
```rust
const BATCH_SIZE: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filters_process.rs (L9-9)
```rust
const BATCH_SIZE: BlockNumber = 1000;
```
