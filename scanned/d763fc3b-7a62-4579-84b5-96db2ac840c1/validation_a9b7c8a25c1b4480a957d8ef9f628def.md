### Title
Unbounded `GetBlockFilterHashes` Flood — No Rate Limiting in Filter Protocol Handler — (`sync/src/filter/get_block_filter_hashes_process.rs`)

---

### Summary

The `BlockFilter` P2P protocol handler has **no rate limiting** of any kind. An unprivileged peer can send an unlimited stream of `GetBlockFilterHashes` messages, each triggering up to **4,001 RocksDB reads** on the victim node, while the attacker's cost per message is a fixed **8-byte payload**. This creates an unbounded asymmetry between attacker bandwidth and victim I/O, causing node resource exhaustion and degraded sync/relay performance.

---

### Finding Description

`GetBlockFilterHashesProcess::execute` performs the following DB work per message:

1. One `get_block_hash(start_number - 1)` + one `get_block_filter_hash(...)` for the parent hash lookup.
2. A loop of up to `BATCH_SIZE = 2000` iterations, each calling `get_block_hash(block_number)` + `get_block_filter_hash(block_hash)` — up to **4,000 additional DB reads**. [1](#0-0) [2](#0-1) 

The `BlockFilter` struct contains **no `rate_limiter` field** and the `received` handler calls `self.process()` directly with no rate check: [3](#0-2) [4](#0-3) 

This is in direct contrast to the `Relayer` protocol, which has an explicit per-peer, per-message-type rate limiter (30 req/sec) checked before any processing: [5](#0-4) [6](#0-5) 

And the `HolePunching` protocol, which similarly has a `rate_limiter` checked in `received`: [7](#0-6) [8](#0-7) 

The `GetBlockFilterHashes` message schema is a single `Uint64` field — 8 bytes on the wire: [9](#0-8) 

Each DB read goes through `ActiveChain::get_block_hash` → `Snapshot::get_block_hash` → RocksDB `COLUMN_INDEX`, and `get_block_filter_hash` → RocksDB `COLUMN_BLOCK_FILTER_HASH`: [10](#0-9) [11](#0-10) [12](#0-11) 

---

### Impact Explanation

Each 8-byte attacker message forces up to **4,001 RocksDB point lookups** on the victim. With varying `start_number` values spaced 2000 blocks apart (e.g., 0, 2000, 4000, …), RocksDB block cache is bypassed, maximizing disk I/O. At 1,000 messages/second (trivially achievable over a single TCP connection), the victim node performs ~4 million DB reads/second, saturating its I/O subsystem and starving the async runtime of capacity for legitimate sync and relay message processing. This degrades or halts the node's participation in the CKB network.

---

### Likelihood Explanation

The attack requires only a single P2P connection to a node with the Filter protocol enabled. No authentication, no PoW, no stake. The `GetBlockFilterHashes` message is a valid, well-formed protocol message. The attacker can sustain the flood indefinitely from a single low-bandwidth connection. The victim has no mechanism to detect or throttle the flood.

---

### Recommendation

Add a per-peer, per-message-type rate limiter to `BlockFilter`, mirroring the pattern already used in `Relayer`:

- Add a `rate_limiter: RateLimiter<(PeerIndex, u32)>` field to `BlockFilter`.
- In `BlockFilter::received`, check `self.rate_limiter.check_key(&(peer_index, msg.item_id()))` before calling `self.process(...)`, returning early (and optionally disconnecting the peer) on excess.
- A quota of 1–5 requests/second per peer per message type is sufficient for legitimate light-client use.

---

### Proof of Concept

```
1. Connect to a CKB node with SupportProtocols::Filter.
2. In a tight loop, send GetBlockFilterHashes messages with start_number cycling
   through 0, 2000, 4000, ..., (tip - 2000) to maximize cache misses.
3. Each message is 8 bytes + framing overhead.
4. Observe on the victim: RocksDB read amplification, CPU saturation in the
   async runtime, and degraded response times for Sync/Relay protocol messages.
5. 1000 messages → ~4,000,000 DB reads; attacker bandwidth: ~8 KB total.
```

### Citations

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L8-8)
```rust
const BATCH_SIZE: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L40-66)
```rust
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

**File:** sync/src/filter/mod.rs (L22-25)
```rust
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
}
```

**File:** sync/src/filter/mod.rs (L122-153)
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

**File:** util/gen-types/schemas/extensions.mol (L221-223)
```text
struct GetBlockFilterHashes {
    start_number:   Uint64,
}
```

**File:** sync/src/types/mod.rs (L1648-1670)
```rust
    pub fn get_block_hash(&self, number: BlockNumber) -> Option<packed::Byte32> {
        self.snapshot().get_block_hash(number)
    }

    pub fn get_block(&self, h: &packed::Byte32) -> Option<core::BlockView> {
        self.store().get_block(h)
    }

    pub fn get_block_header(&self, h: &packed::Byte32) -> Option<core::HeaderView> {
        self.store().get_block_header(h)
    }

    pub fn get_block_ext(&self, h: &packed::Byte32) -> Option<core::BlockExt> {
        self.snapshot().get_block_ext(h)
    }

    pub fn get_block_filter(&self, hash: &packed::Byte32) -> Option<packed::Bytes> {
        self.store().get_block_filter(hash)
    }

    pub fn get_block_filter_hash(&self, hash: &packed::Byte32) -> Option<packed::Byte32> {
        self.store().get_block_filter_hash(hash)
    }
```

**File:** store/src/store.rs (L266-270)
```rust
    fn get_block_hash(&self, number: BlockNumber) -> Option<packed::Byte32> {
        let block_number: packed::Uint64 = number.into();
        self.get(COLUMN_INDEX, block_number.as_slice())
            .map(|raw| packed::Byte32Reader::from_slice_should_be_ok(raw.as_ref()).to_entity())
    }
```

**File:** store/src/store.rs (L492-495)
```rust
    fn get_block_filter_hash(&self, hash: &packed::Byte32) -> Option<packed::Byte32> {
        self.get(COLUMN_BLOCK_FILTER_HASH, hash.as_slice())
            .map(|slice| packed::Byte32Reader::from_slice_should_be_ok(slice.as_ref()).to_entity())
    }
```
