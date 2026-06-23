### Title
Unbounded Response Amplification via Duplicate Indexes in `GetBlockTransactionsProcess::execute` — (`sync/src/relayer/get_block_transactions_process.rs`)

---

### Summary

`GetBlockTransactionsProcess::execute` validates that the number of indexes does not exceed `MAX_RELAY_TXS_NUM_PER_BATCH` (32767), but performs **no deduplication** of those indexes. An unprivileged peer can send 32767 copies of index `0`, causing the node to clone and serialize the same transaction 32767 times into a single `BlockTransactions` response, with no outbound size guard anywhere in the path.

---

### Finding Description

The guard at line 37 only checks the count: [1](#0-0) 

The subsequent `filter_map` at lines 61–71 iterates the raw (non-deduplicated) index list and calls `.cloned()` for each occurrence: [2](#0-1) 

If index `0` appears 32767 times, the `transactions` Vec will contain 32767 clones of the same `TransactionView`. These are then serialized into a single `RelayMessage` at line 95: [3](#0-2) 

`async_send_message_to` has **no size check** — it passes the raw byte buffer directly to the network layer: [4](#0-3) 

The underlying `async_send_message` also performs no size validation before handing off to the tentacle P2P layer: [5](#0-4) 

`MAX_RELAY_TXS_NUM_PER_BATCH` is 32767: [6](#0-5) 

---

### Impact Explanation

CKB's consensus bounds block size to ~597 KB. A single transaction with large witness data can approach that limit. With 32767 duplicate index `0` entries:

- **Memory**: `32767 × ~500 KB ≈ ~16 GB` allocated on the heap before any send attempt.
- **CPU**: Full serialization of 32767 transaction copies into one contiguous buffer.
- **Network layer**: Even if tentacle's internal frame limit rejects the oversized message, the allocation and serialization work has already been done.

A single such message can exhaust available RAM on a typical node, causing OOM termination or severe swap-induced stall — a complete denial of service for that node.

---

### Likelihood Explanation

- Any connected peer can send a `GetBlockTransactions` message; no authentication or privilege is required.
- The precondition (a stored block containing a large transaction) is realistic: large-witness transactions appear on mainnet.
- The rate limiter (30 req/s per peer keyed by `(PeerIndex, message_type)`) does not prevent a single maximally-amplified request from causing the damage; it only limits repetition rate per peer. [7](#0-6) 

---

### Recommendation

Deduplicate indexes before building the response. A minimal fix:

```rust
use std::collections::HashSet;

let mut seen = HashSet::new();
let transactions = self
    .message
    .indexes()
    .iter()
    .filter_map(|i| {
        let idx = Into::<u32>::into(i) as usize;
        if seen.insert(idx) {
            block.transactions().get(idx).cloned()
        } else {
            None
        }
    })
    .collect::<Vec<_>>();
```

Additionally, add an outbound size guard before calling `async_send_message_to`, rejecting or chunking any response whose serialized size exceeds a defined limit (e.g., `MAX_RELAY_TXS_BYTES_PER_BATCH`). [8](#0-7) 

---

### Proof of Concept

1. Identify (or mine) a CKB block containing a transaction with large witness data (~500 KB).
2. Connect to a target node as an unprivileged peer.
3. Send a `GetBlockTransactions` message for that block's hash with `indexes` = `[0u32; 32767]`.
4. Observe the target node's RSS grow by ~16 GB as it clones and serializes 32767 copies of the transaction before attempting to send the response.
5. The node OOMs or becomes unresponsive.

### Citations

**File:** sync/src/relayer/get_block_transactions_process.rs (L37-43)
```rust
            if get_block_transactions.indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "Indexes count({}) > MAX_RELAY_TXS_NUM_PER_BATCH({})",
                    get_block_transactions.indexes().len(),
                    MAX_RELAY_TXS_NUM_PER_BATCH,
                ));
            }
```

**File:** sync/src/relayer/get_block_transactions_process.rs (L61-71)
```rust
            let transactions = self
                .message
                .indexes()
                .iter()
                .filter_map(|i| {
                    block
                        .transactions()
                        .get(Into::<u32>::into(i) as usize)
                        .cloned()
                })
                .collect::<Vec<_>>();
```

**File:** sync/src/relayer/get_block_transactions_process.rs (L80-97)
```rust
            let content = packed::BlockTransactions::new_builder()
                .block_hash(block_hash)
                .transactions(
                    transactions
                        .into_iter()
                        .map(|tx| tx.data())
                        .collect::<Vec<_>>(),
                )
                .uncles(
                    uncles
                        .into_iter()
                        .map(|uncle| uncle.data())
                        .collect::<Vec<_>>(),
                )
                .build();
            let message = packed::RelayMessage::new_builder().set(content).build();

            return async_send_message_to(&self.nc, self.peer, &message).await;
```

**File:** sync/src/utils.rs (L72-101)
```rust
pub(crate) async fn async_send_message<Message: Entity>(
    protocol_id: ProtocolId,
    nc: &Arc<dyn CKBProtocolContext + Sync>,
    peer_index: PeerIndex,
    message: &Message,
) -> Status {
    // ignore Error return, only happens on shutdown case
    if let Err(err) = nc
        .async_send_message(protocol_id, peer_index, message.as_bytes())
        .await
    {
        let name = message_name(protocol_id, message);
        let error_message = format!("nc.send_message {name}, error: {err:?}");
        ckb_logger::error!("{}", error_message);
        return StatusCode::Network.with_context(error_message);
    }

    let bytes = message.as_bytes().len() as u64;
    let item_name = item_name(protocol_id, message);
    let protocol_name = protocol_name(protocol_id);
    metric_ckb_message_bytes(
        MetricDirection::Out,
        &protocol_name,
        &item_name,
        None,
        bytes,
    );

    Status::ok()
}
```

**File:** sync/src/utils.rs (L150-157)
```rust
pub(crate) async fn async_send_message_to<Message: Entity>(
    nc: &Arc<dyn CKBProtocolContext + Sync>,
    peer_index: PeerIndex,
    message: &Message,
) -> Status {
    let protocol_id = nc.protocol_id();
    async_send_message(protocol_id, nc, peer_index, message).await
}
```

**File:** util/constant/src/sync.rs (L68-68)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
```

**File:** sync/src/relayer/mod.rs (L61-61)
```rust
pub const MAX_RELAY_TXS_BYTES_PER_BATCH: usize = 1024 * 1024;
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
