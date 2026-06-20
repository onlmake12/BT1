### Title
Missing Rate Limiter on Filter Protocol Handler Enables DB I/O Amplification DoS — (`sync/src/filter/get_block_filter_check_points_process.rs`)

---

### Summary

The `BlockFilter` protocol handler has no per-peer rate limiter, unlike the `Relayer` handler which explicitly guards every message type. A single unprivileged remote peer can repeatedly send `GetBlockFilterCheckPoints{start_number:0}`, each triggering up to 2000 RocksDB reads with no application-layer throttle, degrading the node's async executor and DB I/O.

---

### Finding Description

`GetBlockFilterCheckPointsProcess::execute()` loops up to `BATCH_SIZE = 2000` iterations, each performing two sequential RocksDB reads: [1](#0-0) 

```
BATCH_SIZE: BlockNumber = 2000
CHECK_POINT_INTERVAL: BlockNumber = 2000
``` [2](#0-1) 

Per iteration: `get_block_hash(block_number)` → `get_block_filter_hash(&block_hash)` = **2 DB reads × 2000 iterations = up to 4,000 RocksDB reads per message**.

The `BlockFilter` handler struct carries no rate limiter: [3](#0-2) 

`try_process()` dispatches directly to the handler with zero rate-limit check: [4](#0-3) 

By contrast, `Relayer` explicitly holds a `governor::RateLimiter` and checks it before every non-PoW message: [5](#0-4) [6](#0-5) 

The Filter protocol is registered and enabled by default via `default_support_all_protocols`: [7](#0-6) [8](#0-7) 

---

### Impact Explanation

On a mainnet node with filter data built (the normal state when Filter is enabled), `start_number=0` always satisfies `latest >= start_number`, so the full 2000-iteration loop always executes. An attacker sending this message at high frequency from a single peer saturates the shared RocksDB I/O path and the Tokio async executor task that processes Filter messages, causing sync and relay message processing to stall. The node becomes unresponsive to legitimate peers. This is a sustained, amplified DoS from a single connection.

---

### Likelihood Explanation

- **Precondition**: Filter protocol enabled (default) and filter data built (automatic background process). Both are true on any standard mainnet node.
- **Attacker requirement**: Any peer that can open a P2P connection — no privilege, no PoW, no key.
- **Message cost**: A 9-byte `GetBlockFilterCheckPoints` struct triggers 4,000 DB reads. The amplification ratio is extreme.
- **No existing guard**: No rate limiter, no per-peer message counter, no cooldown. The only implicit throttle is TCP backpressure, which is insufficient against a local or high-bandwidth attacker.

---

### Recommendation

Add a `governor::RateLimiter<(PeerIndex, u32)>` to `BlockFilter` mirroring the pattern already used in `Relayer::new()`: [9](#0-8) 

Check it at the top of `BlockFilter::try_process()` before dispatching to any handler, and return `StatusCode::TooManyRequests` on excess. A limit of 1–2 requests/second per peer per message type is sufficient for legitimate light-client use.

---

### Proof of Concept

```python
import socket, struct

# Connect to CKB P2P port (default 8115)
# Send repeated GetBlockFilterCheckPoints{start_number: 0}
# Molecule encoding: union tag for GetBlockFilterCheckPoints + uint64 LE 0
msg = b'\x00' * 9  # simplified; real encoding uses molecule framing

while True:
    s = socket.create_connection(("target-node", 8115))
    # negotiate Filter protocol via tentacle handshake, then:
    for _ in range(1000):
        s.send(encode_filter_message(start_number=0))
    # Observe: node DB read latency spikes, sync stalls
```

Each sent message costs the attacker ~9 bytes and forces the victim to execute up to 4,000 RocksDB reads. Repeating at network speed from a single peer is sufficient to degrade a production node. [10](#0-9)

### Citations

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L9-10)
```rust
const BATCH_SIZE: BlockNumber = 2000;
const CHECK_POINT_INTERVAL: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L34-69)
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
            let content = packed::BlockFilterCheckPoints::new_builder()
                .start_number(start_number)
                .block_filter_hashes(block_filter_hashes)
                .build();

            let message = packed::BlockFilterMessage::new_builder()
                .set(content)
                .build();
            async_send_message_to(&self.nc, self.peer, &message).await
        } else {
            Status::ignored()
        }
    }
```

**File:** sync/src/filter/mod.rs (L22-25)
```rust
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
}
```

**File:** sync/src/filter/mod.rs (L50-54)
```rust
            packed::BlockFilterMessageUnionReader::GetBlockFilterCheckPoints(msg) => {
                GetBlockFilterCheckPointsProcess::new(msg, self, nc, peer)
                    .execute()
                    .await
            }
```

**File:** sync/src/relayer/mod.rs (L81-82)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}
```

**File:** sync/src/relayer/mod.rs (L88-98)
```rust
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

**File:** util/app-config/src/configs/network.rs (L82-83)
```rust
    #[serde(default = "default_support_all_protocols")]
    pub support_protocols: Vec<SupportProtocol>,
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
