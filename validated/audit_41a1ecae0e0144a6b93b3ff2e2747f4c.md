### Title
Unbounded Per-Request DB Iteration in Filter Protocol Handlers Enables I/O Exhaustion by Any Connected Peer — (`sync/src/filter/get_block_filter_hashes_process.rs`, `sync/src/filter/get_block_filter_check_points_process.rs`)

---

### Summary

The `GetBlockFilterHashesProcess` and `GetBlockFilterCheckPointsProcess` handlers in the CKB block-filter protocol each iterate up to `BATCH_SIZE = 2000` blocks per single inbound message, performing two RocksDB reads per iteration (up to 4,000 reads per message). The triggering message contains only an 8-byte `start_number` field. There is no per-peer rate limiting in the filter protocol handler. Any connected peer — including an unauthenticated light-client peer — can spam these messages to cause sustained I/O load on the full node, degrading its ability to serve legitimate peers and process blocks.

---

### Finding Description

The `GetBlockFilterHashes` and `GetBlockFilterCheckPoints` messages are defined as structs containing only a single `start_number: Uint64` field: [1](#0-0) 

When a full node receives a `GetBlockFilterHashes` message, `GetBlockFilterHashesProcess::execute` runs a loop of up to `BATCH_SIZE = 2000` iterations: [2](#0-1) [3](#0-2) 

Each iteration calls `active_chain.get_block_hash(block_number)` and then `active_chain.get_block_filter_hash(&block_hash)` — two RocksDB reads — yielding up to **4,000 RocksDB reads per message**.

`GetBlockFilterCheckPointsProcess` has the same `BATCH_SIZE = 2000` and the same two-read-per-iteration pattern: [4](#0-3) [5](#0-4) 

The filter protocol dispatcher (`BlockFilter::try_process`) applies **no per-peer rate limiting** before invoking these handlers: [6](#0-5) 

The analog vulnerability in `GetBlockFiltersProcess` was partially mitigated in v0.203.0 by adding a 1.8 MB response-size cap (CHANGELOG line 124), but that cap only limits **response size**, not the number of DB reads performed before the cap is hit. The `GetBlockFilterHashes` and `GetBlockFilterCheckPoints` handlers received no equivalent mitigation. [7](#0-6) 

---

### Impact Explanation

A malicious peer connected to the filter protocol can send a continuous stream of `GetBlockFilterHashes` or `GetBlockFilterCheckPoints` messages (each 8 bytes of payload), causing the full node to perform thousands of RocksDB reads per message with no throttle. This can:

- Saturate the node's I/O bandwidth and RocksDB read capacity.
- Delay or starve block processing, transaction relay, and legitimate sync peers that share the same I/O path.
- Degrade node availability for honest light clients and full peers.

The work-to-message-size ratio is extreme: 8 bytes in → up to 4,000 DB reads out.

---

### Likelihood Explanation

The filter protocol (`SupportProtocols::Filter`) is reachable by any peer that connects to the node. No authentication or stake is required. A single attacker with one TCP connection can sustain the attack indefinitely by sending `GetBlockFilterHashes` messages in a tight loop. The attack is cheap for the attacker and expensive for the victim.

---

### Recommendation

1. **Add per-peer rate limiting** in `BlockFilter::try_process` (or in the individual handlers) to cap the number of filter-protocol requests processed per peer per time window.
2. **Reduce `BATCH_SIZE`** for `GetBlockFilterHashes` and `GetBlockFilterCheckPoints` to a smaller value (e.g., 200–500), consistent with the 1.8 MB cap already applied to `GetBlockFilters`.
3. Consider **banning peers** that send requests at an abusive rate, analogous to the `BAD_MESSAGE_BAN_TIME` already imported in `sync/src/filter/mod.rs`. [8](#0-7) 

---

### Proof of Concept

1. Connect a peer to a full node's filter protocol endpoint (`SupportProtocols::Filter`).
2. In a tight loop, send `GetBlockFilterHashes { start_number: 0 }` (8-byte payload).
3. Each message causes the node to execute up to 2,000 iterations of `get_block_hash` + `get_block_filter_hash` (4,000 RocksDB reads).
4. Observe sustained I/O saturation on the full node; legitimate sync and relay operations are delayed.

The triggering message structure: [9](#0-8) 

The vulnerable loop (no rate gate before entry): [10](#0-9)

### Citations

**File:** util/gen-types/schemas/extensions.mol (L221-238)
```text
struct GetBlockFilterHashes {
    start_number:   Uint64,
}

table BlockFilterHashes {
    start_number:               Uint64,
    parent_block_filter_hash:   Byte32,
    block_filter_hashes:        Byte32Vec,
}

struct GetBlockFilterCheckPoints {
    start_number:   Uint64,
}

table BlockFilterCheckPoints {
    start_number:           Uint64,
    block_filter_hashes:    Byte32Vec,
}
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L8-8)
```rust
const BATCH_SIZE: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L32-80)
```rust
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
            let content = packed::BlockFilterHashes::new_builder()
                .start_number(start_number)
                .parent_block_filter_hash(parent_block_filter_hash)
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

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L9-10)
```rust
const BATCH_SIZE: BlockNumber = 2000;
const CHECK_POINT_INTERVAL: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L42-56)
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
                let Some(next_block_number) = block_number.checked_add(CHECK_POINT_INTERVAL) else {
                    break;
                };
                block_number = next_block_number;
            }
```

**File:** sync/src/filter/mod.rs (L11-11)
```rust
use ckb_constant::sync::BAD_MESSAGE_BAN_TIME;
```

**File:** sync/src/filter/mod.rs (L33-68)
```rust
    async fn try_process(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        message: packed::BlockFilterMessageUnionReader<'_>,
    ) -> Status {
        match message {
            packed::BlockFilterMessageUnionReader::GetBlockFilters(msg) => {
                GetBlockFiltersProcess::new(msg, self, nc, peer)
                    .execute()
                    .await
            }
            packed::BlockFilterMessageUnionReader::GetBlockFilterHashes(msg) => {
                GetBlockFilterHashesProcess::new(msg, self, nc, peer)
                    .execute()
                    .await
            }
            packed::BlockFilterMessageUnionReader::GetBlockFilterCheckPoints(msg) => {
                GetBlockFilterCheckPointsProcess::new(msg, self, nc, peer)
                    .execute()
                    .await
            }
            packed::BlockFilterMessageUnionReader::BlockFilters(_)
            | packed::BlockFilterMessageUnionReader::BlockFilterHashes(_)
            | packed::BlockFilterMessageUnionReader::BlockFilterCheckPoints(_) => {
                // remote peer should not send block filter to us without asking
                // TODO: ban remote peer
                warn_target!(
                    crate::LOG_TARGET_FILTER,
                    "Received unexpected message from peer: {:?}",
                    peer
                );
                Status::ignored()
            }
        }
    }
```

**File:** sync/src/filter/get_block_filters_process.rs (L45-57)
```rust
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
```
