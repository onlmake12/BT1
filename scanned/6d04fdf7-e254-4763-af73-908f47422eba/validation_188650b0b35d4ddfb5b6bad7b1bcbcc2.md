### Title
Missing Index Deduplication and Response Size Cap in `GetBlockTransactionsProcess::execute` Enables OOM via Amplified Response — (`sync/src/relayer/get_block_transactions_process.rs`)

---

### Summary

`GetBlockTransactionsProcess::execute` accepts up to 32,767 transaction indexes per message, does not deduplicate them, and applies no byte-size cap to the response it builds and sends. An unprivileged remote peer can send 32,767 duplicate indexes pointing to a single large transaction in a known block, causing the node to allocate a response of up to ~19.5 GB per request. At the rate-limiter ceiling of 30 req/sec, this causes rapid memory exhaustion and node crash.

---

### Finding Description

In `execute()`, two guards exist:

```
if get_block_transactions.indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH { ... }
if get_block_transactions.uncle_indexes().len() > shared.consensus().max_uncles_num() { ... }
```

`MAX_RELAY_TXS_NUM_PER_BATCH = 32767`, so exactly 32,767 indexes pass the check. [1](#0-0) [2](#0-1) 

After the guard, the code iterates every index with a plain `filter_map` and no deduplication:

```rust
let transactions = self
    .message
    .indexes()
    .iter()
    .filter_map(|i| {
        block.transactions().get(Into::<u32>::into(i) as usize).cloned()
    })
    .collect::<Vec<_>>();
``` [3](#0-2) 

If the attacker sends `[0, 0, 0, …, 0]` (32,767 zeros) and the block has a large transaction at index 0, the `Vec` will contain 32,767 clones of that transaction. The response is then built and sent with no byte-size check: [4](#0-3) 

`async_send_message_to` performs no size validation before enqueuing: [5](#0-4) 

**Contrast with `GetBlockProposalProcess`**, which explicitly deduplicates via `HashSet`, rejects duplicate requests with `StatusCode::RequestDuplicate`, and enforces `MAX_RELAY_TXS_BYTES_PER_BATCH` (1 MB) on the response: [6](#0-5) [7](#0-6) 

`GetBlockTransactionsProcess` has **neither** protection. The grep for `MAX_RELAY_TXS_BYTES_PER_BATCH` in `get_block_transactions_process.rs` returns zero matches, confirming the omission.

---

### Impact Explanation

`MAX_BLOCK_BYTES = TWO_IN_TWO_OUT_BYTES × TWO_IN_TWO_OUT_COUNT = 597 × 1,000 = 597,000 bytes`. [8](#0-7) 

A single transaction can occupy up to ~597 KB of a block. With 32,767 duplicate indexes:

- **Per-response allocation**: 32,767 × 597,000 ≈ **~19.5 GB**
- **At 30 req/sec**: the node attempts to allocate and enqueue ~585 GB/sec of outbound data

Even a single such request will OOM a typical node before the send completes. The rate limiter at 30/sec sustains the attack. Impact: **local node crash from memory exhaustion or send-queue saturation**.

---

### Likelihood Explanation

- Requires only an unprivileged P2P connection — no PoW, no key, no trusted role.
- The attacker needs only to know the hash of any block with a large transaction (publicly observable on-chain).
- The rate limiter (30/sec) is the only throttle, and it is the amplifier, not a mitigator.
- The attack is deterministic and locally reproducible.

---

### Recommendation

1. **Deduplicate indexes** before the `filter_map`, rejecting or deduplicating duplicate entries (as `GetBlockProposalProcess` does with `HashSet`).
2. **Enforce a byte-size cap** on the response (e.g., `MAX_RELAY_TXS_BYTES_PER_BATCH = 1 MB`) and split or truncate if exceeded.
3. Optionally lower `MAX_RELAY_TXS_NUM_PER_BATCH` for this handler or add a per-response size pre-check before allocating.

---

### Proof of Concept

```
1. Connect to a CKB node as an unprivileged peer on RelayV3.
2. Identify any block hash B where block.transactions[0] is large (e.g., ~500 KB).
3. Construct a GetBlockTransactions message:
     block_hash = B
     indexes    = [0u32; 32767]   // 32,767 copies of index 0
     uncle_indexes = []
4. Send 30 such messages per second (rate-limiter ceiling).
5. Observe: node allocates ~32,767 × tx_size bytes per response;
   RSS grows rapidly; node OOMs or send queue saturates → crash.
```

### Citations

**File:** sync/src/relayer/mod.rs (L60-61)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
pub const MAX_RELAY_TXS_BYTES_PER_BATCH: usize = 1024 * 1024;
```

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

**File:** sync/src/relayer/get_block_proposal_process.rs (L47-51)
```rust
        let proposals: HashSet<packed::ProposalShortId> =
            self.message.proposals().to_entity().into_iter().collect();

        if proposals.len() != message_len {
            return StatusCode::RequestDuplicate.with_context("Request duplicate proposal");
```

**File:** sync/src/relayer/get_block_proposal_process.rs (L79-95)
```rust
        let mut relay_bytes = 0;
        let mut relay_proposals = Vec::new();
        for (_, tx) in fetched_transactions {
            let data = tx.data();
            let tx_size = data.total_size();
            if relay_bytes + tx_size > MAX_RELAY_TXS_BYTES_PER_BATCH {
                self.send_block_proposals(std::mem::take(&mut relay_proposals))
                    .await;
                relay_bytes = tx_size;
            } else {
                relay_bytes += tx_size;
            }
            relay_proposals.push(data);
        }
        if !relay_proposals.is_empty() {
            attempt!(self.send_block_proposals(relay_proposals).await);
        }
```

**File:** spec/src/consensus.rs (L82-84)
```rust
/// The default maximum allowed size in bytes for a block
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```
