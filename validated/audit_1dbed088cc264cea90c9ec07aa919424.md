All code references check out. The vulnerability is confirmed valid.

Audit Report

## Title
Missing Duplicate Index Check in `GetBlockTransactionsProcess::execute()` Enables Per-Message Bandwidth Amplification — (`File: sync/src/relayer/get_block_transactions_process.rs`)

## Summary
`GetBlockTransactionsProcess::execute()` validates only the *count* of `indexes` and `uncle_indexes` against configured limits but never checks for duplicate values within those lists. An attacker peer can send a single `GetBlockTransactions` message with up to 32,767 identical transaction indexes, causing the node to clone, serialize, and transmit the same `TransactionView` 32,767 times in one `BlockTransactions` response. This is a direct, repeatable bandwidth amplification primitive against any CKB relay node.

## Finding Description
In `GetBlockTransactionsProcess::execute()`, the only input validation on `indexes` is a count ceiling check: [1](#0-0) 

No uniqueness check follows. The handler then iterates the raw `indexes` list directly, cloning each matching `TransactionView`: [2](#0-1) 

The same pattern applies to `uncle_indexes`, bounded only by `max_uncles_num` (2 on mainnet) with no dedup: [3](#0-2) 

`MAX_RELAY_TXS_NUM_PER_BATCH` is 32,767: [4](#0-3) 

Every other analogous relay handler performs a deduplication check before processing. `GetTransactionsProcess` builds a `HashSet` and rejects if `message_len != tx_hashes_set.len()`: [5](#0-4) 

`GetBlockProposalProcess` converts to a `HashSet` and rejects if `proposals.len() != message_len`: [6](#0-5) 

`GetBlocksProcess` uses a `dedup` `HashSet` and returns `StatusCode::RequestDuplicate` on collision: [7](#0-6) 

`GetBlockTransactionsProcess` is the sole handler that omits this guard.

A per-peer rate limiter exists in the `Relayer` at 30 requests/second per `(PeerIndex, message_type)` pair: [8](#0-7) [9](#0-8) 

This limits message frequency but does **not** bound the amplification ratio within each message. Each of the 30 allowed messages per second can carry 32,767 duplicate indexes, so the rate limiter does not mitigate the core amplification.

## Impact Explanation
**Impact: High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

Per attacker peer: 30 messages/sec × 32,767 indexes × serialized transaction size. For a typical 597-byte transaction (`TWO_IN_TWO_OUT_BYTES`), this yields approximately **587 MB/sec of outbound bandwidth per attacker peer**. With `MAX_RELAY_PEERS = 128`, coordinated abuse from multiple connections can saturate the victim node's outbound relay capacity, preventing it from serving legitimate peers and causing effective network congestion. The attacker's cost is a constant-size inbound message stream; the victim's cost scales with `32767 × tx_size` per message. [10](#0-9) 

## Likelihood Explanation
**Likelihood: High.** The `GetBlockTransactions` message is part of the public `RelayV3` protocol. Any peer completing the standard handshake — no authentication, no stake, no special role — can send it. The attacker needs only one valid `block_hash` (trivially obtained from any block explorer or `get_tip_block_hash` RPC) and one valid transaction index (index 0, the cellbase, always exists in every committed block). No hash collision, no key material, and no privileged access is required. The attack is repeatable indefinitely within the rate limit window.

## Recommendation
Add a duplicate-index check immediately after the count validation in `GetBlockTransactionsProcess::execute()`, consistent with every other relay handler:

```rust
use std::collections::HashSet;

let indexes_set: HashSet<u32> = self.message.indexes().iter()
    .map(Into::<u32>::into).collect();
if indexes_set.len() != self.message.indexes().len() {
    return StatusCode::ProtocolMessageIsMalformed
        .with_context("Duplicate indexes in GetBlockTransactions");
}

let uncle_indexes_set: HashSet<u32> = self.message.uncle_indexes().iter()
    .map(Into::<u32>::into).collect();
if uncle_indexes_set.len() != self.message.uncle_indexes().len() {
    return StatusCode::ProtocolMessageIsMalformed
        .with_context("Duplicate uncle_indexes in GetBlockTransactions");
}
```

This mirrors the pattern already used in `GetTransactionsProcess`, `GetBlockProposalProcess`, and `GetBlocksProcess`.

## Proof of Concept
1. Connect to any CKB mainnet/testnet node as a relay peer and complete the `RelayV3` handshake.
2. Obtain any committed block hash `H` with at least one transaction (index 0, the cellbase, always exists).
3. Craft a `GetBlockTransactions` message:
   - `block_hash = H`
   - `indexes = [0, 0, 0, …, 0]` repeated 32,767 times
   - `uncle_indexes = []`
4. Send the message. The node passes the count check (`32767 <= MAX_RELAY_TXS_NUM_PER_BATCH`) and proceeds to clone the cellbase transaction 32,767 times, serialize all copies, and transmit a `BlockTransactions` response of `32767 × sizeof(cellbase_tx)` bytes.
5. Repeat at 30 messages/second (within the rate limit) to sustain continuous amplified outbound bandwidth consumption on the victim node.

The root cause is confirmed at `sync/src/relayer/get_block_transactions_process.rs` lines 61–71, where the raw `indexes` iterator is consumed without deduplication. [2](#0-1)

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

**File:** sync/src/relayer/get_block_transactions_process.rs (L73-78)
```rust
            let uncles = self
                .message
                .uncle_indexes()
                .iter()
                .filter_map(|i| block.uncles().get(Into::<u32>::into(i) as usize))
                .collect::<Vec<_>>();
```

**File:** sync/src/relayer/mod.rs (L59-61)
```rust
pub const MAX_RELAY_PEERS: usize = 128;
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
pub const MAX_RELAY_TXS_BYTES_PER_BATCH: usize = 1024 * 1024;
```

**File:** sync/src/relayer/mod.rs (L88-92)
```rust
    pub fn new(chain: ChainController, shared: Arc<SyncShared>) -> Self {
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
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

**File:** sync/src/relayer/get_transactions_process.rs (L54-61)
```rust
            let tx_hashes_set: HashSet<_> = tx_hashes
                .iter()
                .map(|tx_hash| packed::ProposalShortId::from_tx_hash(&tx_hash.to_entity()))
                .collect();

            if message_len != tx_hashes_set.len() {
                return StatusCode::RequestDuplicate.with_context("Request duplicate transaction");
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

**File:** sync/src/synchronizer/get_blocks_process.rs (L47-58)
```rust
        let mut dedup = HashSet::new();
        for block_hash in iter {
            debug!("get_blocks {} from peer {:?}", block_hash, self.peer);
            let block_hash = block_hash.to_entity();

            if block_hash == self.synchronizer.shared().consensus().genesis_hash() {
                return StatusCode::RequestGenesis.with_context("Request genesis block");
            }

            if !dedup.insert(block_hash.clone()) {
                return StatusCode::RequestDuplicate.with_context("Request duplicate block");
            }
```
