Based on my code investigation, here is my assessment:

---

### Title
Missing Rate Limiter on Filter Protocol Enables DB Read Amplification — (`sync/src/filter/mod.rs`, `sync/src/filter/get_block_filter_check_points_process.rs`)

### Summary

`GetBlockFilterCheckPointsProcess::execute` performs up to 4,000 RocksDB point reads per single P2P message with no per-peer rate limiting. The Filter protocol handler has no rate limiter, unlike the Relay and HolePunching protocol handlers which both implement one. Any unprivileged peer can repeatedly send `GetBlockFilterCheckPoints(start_number=0)` to flood the DB I/O path.

### Finding Description

`BATCH_SIZE = 2000` and `CHECK_POINT_INTERVAL = 2000` are hardcoded constants. [1](#0-0) 

The loop runs up to 2000 iterations, each performing two RocksDB point reads: `get_block_hash` (COLUMN_NUMBER_HASH) and `get_block_filter_hash` (COLUMN_BLOCK_FILTER_HASH). [2](#0-1) 

On mainnet with ~4M blocks, `4,000,000 / 2000 = 2000` iterations hit the full `BATCH_SIZE` cap, yielding exactly 4,000 DB reads per message.

The `BlockFilter` handler struct carries only `shared: Arc<SyncShared>` — no rate limiter field exists. [3](#0-2) 

The `received` → `process` → `try_process` call chain has no rate-limit check before dispatching to `GetBlockFilterCheckPointsProcess::execute`. [4](#0-3) 

Contrast with the Relay protocol, which gates every non-PoW message through a `governor`-based rate limiter keyed by `(PeerIndex, message_type)`: [5](#0-4) 

And the HolePunching protocol, which has both a per-peer rate limiter and a per-route forward rate limiter: [6](#0-5) 

The Filter protocol is enabled by default in the shipped configuration: [7](#0-6) 

And the block filter service is started whenever `Filter` is in `support_protocols`: [8](#0-7) 

### Impact Explanation

A peer connecting on protocol ID 121 (`/ckb/filter`) can send `GetBlockFilterCheckPoints(start_number=0)` in a tight loop. Each 8-byte message triggers up to 4,000 synchronous RocksDB reads. With `max_peers = 125`, an attacker controlling or spoofing many peers can aggregate hundreds of thousands of DB reads per second against a single node, saturating RocksDB I/O and stalling block verification and sync. The impact is per-node performance degradation, not a network-wide crash — the "crash the whole CKB network" framing in the question is overstated.

### Likelihood Explanation

The Filter protocol is on by default, the message requires no PoW, no authentication, and no prior state. The attacker only needs to establish a P2P connection and send a valid 8-byte molecule-encoded message. This is trivially scriptable.

### Recommendation

Add a `governor`-based rate limiter to `BlockFilter` keyed by `(PeerIndex, message_item_id)`, matching the pattern already used in `Relayer` and `HolePunching`. A limit of ~5–10 `GetBlockFilterCheckPoints` requests per peer per second is sufficient for legitimate light-client sync while eliminating the amplification vector.

### Proof of Concept

On a node with 4M+ blocks and Filter enabled:
1. Connect 50+ peers to the target node on protocol `/ckb/filter`.
2. Each peer sends `GetBlockFilterCheckPoints { start_number: 0 }` in a tight loop.
3. Observe RocksDB read latency via metrics; block processing throughput drops measurably as the DB I/O queue saturates.

### Citations

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L9-10)
```rust
const BATCH_SIZE: BlockNumber = 2000;
const CHECK_POINT_INTERVAL: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L43-56)
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

**File:** resource/ckb.toml (L112-112)
```text
support_protocols = ["Ping", "Discovery", "Identify", "Feeler", "DisconnectMessage", "Sync", "Relay", "Time", "Alert", "LightClient", "Filter", "HolePunching"]
```

**File:** util/launcher/src/lib.rs (L262-273)
```rust
    /// start block filter service
    pub fn start_block_filter(&self, shared: &Shared) {
        if self
            .args
            .config
            .network
            .support_protocols
            .contains(&SupportProtocol::Filter)
        {
            BlockFilterService::new(shared.clone()).start();
        }
    }
```
