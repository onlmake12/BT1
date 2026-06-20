The code is clear. Let me verify one more thing — whether there's any network-level rate limiting applied to the Filter protocol specifically.

### Title
Missing Per-Peer Rate Limiting on `GetBlockFilterCheckPoints` Enables DB Read Amplification DoS — (`sync/src/filter/get_block_filter_check_points_process.rs`)

---

### Summary

The `BlockFilter` protocol handler processes `GetBlockFilterCheckPoints` messages with no per-peer rate limiting. Any connected peer can send this message in a tight loop, each time triggering up to 4,000 RocksDB point-lookups and generating ~64 KB of outbound data, with no ban, no throttle, and no cost to the attacker.

---

### Finding Description

`GetBlockFilterCheckPointsProcess::execute` iterates up to `BATCH_SIZE=2000` times, performing two DB reads per iteration (`get_block_hash` + `get_block_filter_hash`), whenever `latest >= start_number`. [1](#0-0) [2](#0-1) 

With `start_number=0`, the guard `if latest >= start_number` is satisfied as soon as any filter data exists (i.e., `latest >= 0`), which is the normal operating state of any node with the Filter protocol enabled. [3](#0-2) 

The `BlockFilter` handler struct carries no rate limiter: [4](#0-3) 

The `process` dispatch path applies no rate check before calling `execute`: [5](#0-4) 

`execute` only ever returns `Status::ok()` or `Status::ignored()` — never a ban-triggering status — so `process`'s ban branch is never reached for this message type: [6](#0-5) 

**Contrast with `Relayer`**, which has an explicit `rate_limiter: RateLimiter<(PeerIndex, u32)>` and checks it before dispatching any message: [7](#0-6) [8](#0-7) 

`HolePunching` similarly has both a `rate_limiter` and a `forward_rate_limiter`: [9](#0-8) 

The Filter protocol is included in the default protocol set: [10](#0-9) 

---

### Impact Explanation

Per message sent by the attacker:
- Up to **4,000 RocksDB point-lookups** (`get_block_hash` + `get_block_filter_hash` × 2000 iterations)
- Up to **~64 KB outbound** (2000 × 32-byte hashes in the response)

With no rate limit, a single peer can sustain thousands of such requests per second, causing:
1. **Sustained DB read amplification** — RocksDB I/O pressure degrades block processing and sync throughput
2. **Outbound bandwidth congestion** — the node's upload capacity is consumed serving attacker-controlled requests

---

### Likelihood Explanation

The Filter protocol is on by default. Any peer that connects and negotiates the `/ckb/filter` protocol can immediately begin flooding. No PoW, no stake, no privileged role is required. The attacker message is 9 bytes (`GetBlockFilterCheckPoints` with a `u64` field). The exploit is trivially reproducible locally.

---

### Recommendation

Add a `rate_limiter: RateLimiter<(PeerIndex, u32)>` field to `BlockFilter` (mirroring `Relayer`) and check it at the top of `try_process` before dispatching to any handler. A limit of 1–5 requests/second per peer per message type is sufficient for legitimate light-client use.

---

### Proof of Concept

```rust
// Connect to a CKB node with Filter protocol enabled and ≥1 block of filter data
// Then in a tight loop:
let msg = packed::BlockFilterMessage::new_builder()
    .set(
        packed::GetBlockFilterCheckPoints::new_builder()
            .start_number(0u64)
            .build()
    )
    .build();

loop {
    net.send(&node, SupportProtocols::Filter, msg.as_bytes());
    // No sleep — no rate limit on the server side
    // Each iteration triggers 2000×get_block_hash + 2000×get_block_filter_hash
    // and returns ~64 KB. Peer is never banned.
}
```

Assert: peer remains connected indefinitely; DB read counters increase proportionally to send rate; no `ban_peer` is called.

### Citations

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L9-10)
```rust
const BATCH_SIZE: BlockNumber = 2000;
const CHECK_POINT_INTERVAL: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L37-56)
```rust
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

**File:** sync/src/filter/mod.rs (L21-25)
```rust
#[derive(Clone)]
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

**File:** sync/src/filter/mod.rs (L88-97)
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
```

**File:** sync/src/relayer/mod.rs (L81-82)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L45-46)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
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
