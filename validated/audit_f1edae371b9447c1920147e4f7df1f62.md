All code references check out. Here is the validation result:

Audit Report

## Title
Missing Index Deduplication and Response Size Cap in `GetBlockTransactionsProcess::execute` Enables OOM via Amplified Response — (`sync/src/relayer/get_block_transactions_process.rs`)

## Summary
`GetBlockTransactionsProcess::execute` accepts up to 32,767 transaction indexes per message, performs no deduplication, and applies no byte-size cap before building and sending the response. An unprivileged remote peer can send 32,767 duplicate indexes pointing to a single large transaction in a known block, causing the node to allocate a response of potentially ~19.5 GB per request, leading to rapid memory exhaustion and node crash.

## Finding Description
`MAX_RELAY_TXS_NUM_PER_BATCH = 32767` is defined at [1](#0-0)  The count guard at [2](#0-1)  uses strict `>`, so exactly 32,767 indexes are permitted through. After the guard, the code iterates every index with a plain `filter_map` and no deduplication: [3](#0-2)  If the attacker sends `[0u32; 32767]` and `block.transactions()[0]` is a large transaction, the `Vec` will contain 32,767 clones of that transaction. The response is then built and sent with no byte-size check: [4](#0-3)  `async_send_message_to` performs no size validation before enqueuing: [5](#0-4)  By contrast, `GetBlockProposalProcess` explicitly deduplicates via `HashSet` and rejects duplicates: [6](#0-5)  and enforces `MAX_RELAY_TXS_BYTES_PER_BATCH` (1 MB) on the response: [7](#0-6)  `GetBlockTransactionsProcess` imports only `MAX_RELAY_TXS_NUM_PER_BATCH` — `MAX_RELAY_TXS_BYTES_PER_BATCH` is never imported or used: [8](#0-7) 

## Impact Explanation
`MAX_BLOCK_BYTES = TWO_IN_TWO_OUT_BYTES × TWO_IN_TWO_OUT_COUNT = 597 × 1000 = 597,000 bytes ≈ 597 KB`: [9](#0-8)  A single transaction can occupy up to ~597 KB of a block. With 32,767 duplicate indexes, a single response requires 32,767 × ~597,000 bytes ≈ **~19.5 GB** of allocation. At the rate-limiter ceiling of 30 req/sec, the node attempts to allocate and enqueue ~585 GB/sec of outbound data. Even a single such request will OOM a typical node before the send completes. This matches the **High** impact: *"Vulnerabilities which could easily crash a CKB node."*

## Likelihood Explanation
- Requires only an unprivileged P2P connection — no PoW, no key, no trusted role.
- The attacker needs only to know the hash of any block with a large transaction, which is publicly observable on-chain.
- The rate limiter (30 req/sec, keyed by `(PeerIndex, message.item_id())`) is the only throttle and acts as an amplifier, not a mitigator: [10](#0-9) 
- The attack is deterministic and locally reproducible.

## Recommendation
1. **Deduplicate indexes** before the `filter_map`, using a `HashSet<u32>` and rejecting or silently deduplicating duplicate entries, mirroring `GetBlockProposalProcess`.
2. **Enforce a byte-size cap** on the response using `MAX_RELAY_TXS_BYTES_PER_BATCH` (1 MB), truncating or splitting the response if exceeded.
3. Optionally lower `MAX_RELAY_TXS_NUM_PER_BATCH` for this handler or add a per-response size pre-check before allocating the `Vec`.

## Proof of Concept
```
1. Connect to a CKB node as an unprivileged peer on RelayV3.
2. Identify any block hash B where block.transactions[0] is large (e.g., ~500 KB).
3. Construct a GetBlockTransactions message:
     block_hash    = B
     indexes       = [0u32; 32767]   // 32,767 copies of index 0
     uncle_indexes = []
4. Send 30 such messages per second (rate-limiter ceiling per peer per message type).
5. Observe: node allocates ~32,767 × tx_size bytes per response;
   RSS grows rapidly; node OOMs or send queue saturates → crash.
```

### Citations

**File:** sync/src/relayer/mod.rs (L60-61)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
pub const MAX_RELAY_TXS_BYTES_PER_BATCH: usize = 1024 * 1024;
```

**File:** sync/src/relayer/mod.rs (L89-92)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** sync/src/relayer/get_block_transactions_process.rs (L1-1)
```rust
use crate::relayer::{MAX_RELAY_TXS_NUM_PER_BATCH, Relayer};
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

**File:** sync/src/relayer/get_block_proposal_process.rs (L47-52)
```rust
        let proposals: HashSet<packed::ProposalShortId> =
            self.message.proposals().to_entity().into_iter().collect();

        if proposals.len() != message_len {
            return StatusCode::RequestDuplicate.with_context("Request duplicate proposal");
        }
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

**File:** spec/src/consensus.rs (L72-83)
```rust
pub const TWO_IN_TWO_OUT_BYTES: u64 = 597;
/// count of two-in-two-out txs a block should capable to package.
const TWO_IN_TWO_OUT_COUNT: u64 = 1_000;
pub(crate) const DEFAULT_EPOCH_DURATION_TARGET: u64 = 4 * 60 * 60; // 4 hours, unit: second
const MILLISECONDS_IN_A_SECOND: u64 = 1000;
const MAX_EPOCH_LENGTH: u64 = DEFAULT_EPOCH_DURATION_TARGET / MIN_BLOCK_INTERVAL; // 1800
const MIN_EPOCH_LENGTH: u64 = DEFAULT_EPOCH_DURATION_TARGET / MAX_BLOCK_INTERVAL; // 300
pub(crate) const DEFAULT_PRIMARY_EPOCH_REWARD_HALVING_INTERVAL: EpochNumber =
    4 * 365 * 24 * 60 * 60 / DEFAULT_EPOCH_DURATION_TARGET; // every 4 years

/// The default maximum allowed size in bytes for a block
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
```
