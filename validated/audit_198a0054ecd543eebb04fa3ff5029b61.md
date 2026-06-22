### Title
Unbounded Allocation Before Pending-Block Guard in `BlockTransactionsProcess::execute()` — (File: sync/src/relayer/block_transactions_process.rs)

### Summary
`BlockTransactionsProcess::execute()` fully deserializes and allocates all `TransactionView` and `UncleBlockView` objects from a peer-supplied `BlockTransactions` P2P message **before** checking whether the referenced `block_hash` is present in `pending_compact_blocks`. A malicious peer can send crafted `BlockTransactions` messages with an arbitrary number of large transactions, forcing the node to perform unbounded heap allocation and CPU work that is discarded immediately when the guard check fails.

### Finding Description
In `sync/src/relayer/block_transactions_process.rs`, `BlockTransactionsProcess::execute()` begins by calling `self.message.to_entity()` to produce an owned `BlockTransactions` value, then immediately iterates over every transaction and uncle to build `Vec<TransactionView>` and `Vec<UncleBlockView>`:

```rust
// Lines 48–59 — allocations happen unconditionally
let block_transactions = self.message.to_entity();
let block_hash = block_transactions.block_hash();
let received_transactions: Vec<core::TransactionView> = block_transactions
    .transactions()
    .into_iter()
    .map(|tx| tx.into_view())   // allocates a TransactionView per tx
    .collect();
let received_uncles: Vec<core::UncleBlockView> = block_transactions
    .uncles()
    .into_iter()
    .map(|uncle| uncle.into_view())  // allocates an UncleBlockView per uncle
    .collect();
```

Only after all of this work does the code check whether the block is actually pending:

```rust
// Line 65 — guard that should have come first
if let Entry::Occupied(mut pending) = shared
    .state()
    .pending_compact_blocks()
    .await
    .entry(block_hash.clone())
{ ... }
// Falls through to Status::ignored() if block_hash is unknown
```

If `block_hash` is not in `pending_compact_blocks` (e.g., it is a random hash invented by the attacker), the function returns `Status::ignored()` and all allocated memory is dropped — but the CPU and heap work has already been done.

There is **no count check** on `transactions()` or `uncles()` before the allocation loop. Compare this to every other relay handler, which guards count before allocating:

- `GetBlockTransactionsProcess`: checks `indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH` first
- `TransactionHashesProcess`: checks `tx_hashes().len() > MAX_RELAY_TXS_NUM_PER_BATCH` first
- `GetTransactionsProcess`: checks `tx_hashes().len() > MAX_RELAY_TXS_NUM_PER_BATCH` first
- `BlockProposalProcess`: checks `transactions().len() > limit` first

`BlockTransactionsProcess` is the only relay handler that skips this guard. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

### Impact Explanation
A malicious peer sends repeated `BlockTransactions` P2P messages containing a random `block_hash` and the maximum number of large transactions the P2P framing allows (the TCP relay codec is capped at 2 MB per message). For each message the victim node:

1. Deserializes the full molecule payload (`to_entity()`)
2. Allocates a `TransactionView` for every transaction in the message
3. Allocates an `UncleBlockView` for every uncle in the message
4. Checks `pending_compact_blocks` — finds nothing — drops everything

The rate limiter in `Relayer::try_process()` permits 30 `BlockTransactions` messages per second per peer. At 30 req/s × 2 MB each, a single attacker connection can force ~60 MB/s of allocation-and-free churn, sustained indefinitely. With multiple connections this scales linearly. The result is elevated CPU (allocation, GC pressure, cache thrashing) and potential memory exhaustion on resource-constrained nodes, degrading or halting block relay. [6](#0-5) [7](#0-6) [8](#0-7) 

### Likelihood Explanation
Any peer that can establish a P2P connection — which requires no credentials — can send `BlockTransactions` messages. The `BlockTransactions` message type is a standard relay protocol message; no prior handshake or block request is required from the attacker's side. The attack is trivially scriptable: open a connection, repeatedly send a crafted `BlockTransactions` message with a random hash and maximum-size transaction list. The rate limiter (30/s) does not prevent the attack; it only bounds the per-peer rate, which is still sufficient to cause measurable resource consumption. [9](#0-8) 

### Recommendation
Add a count guard at the top of `BlockTransactionsProcess::execute()`, before `self.message.to_entity()`, mirroring the pattern used in every other relay handler:

```rust
pub async fn execute(self) -> Status {
    let shared = self.relayer.shared();
    // Guard BEFORE any allocation
    if self.message.transactions().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
        return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
            "BlockTransactions tx count({}) > MAX_RELAY_TXS_NUM_PER_BATCH({})",
            self.message.transactions().len(),
            MAX_RELAY_TXS_NUM_PER_BATCH,
        ));
    }
    if self.message.uncles().len() > shared.consensus().max_uncles_num() {
        return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
            "BlockTransactions uncle count({}) > max_uncles_num({})",
            self.message.uncles().len(),
            shared.consensus().max_uncles_num(),
        ));
    }
    // ... existing code
```

Additionally, consider moving the `pending_compact_blocks` existence check to operate on the raw reader (before `to_entity()`) so that the full deserialization is skipped entirely for unknown block hashes. [10](#0-9) [6](#0-5) 

### Proof of Concept
1. Connect to a CKB node as a P2P peer using the RelayV3 protocol.
2. Construct a `BlockTransactions` molecule message with:
   - `block_hash`: any 32-byte value not corresponding to a pending compact block
   - `transactions`: fill with as many minimal-but-valid `Transaction` entries as fit within the 2 MB P2P frame
   - `uncles`: empty
3. Send this message at the maximum rate permitted by the rate limiter (30/s).
4. Observe via process monitoring (`/proc/<pid>/status`, `perf stat`) that the node's heap allocation rate and CPU usage increase proportionally to the number of transactions per message, with no corresponding useful work performed.
5. Repeat from multiple peer connections to scale the effect linearly.

The node will allocate and immediately free a `Vec<TransactionView>` for every message, with no ban or disconnect triggered (the handler returns `Status::ignored()`, not a ban-worthy status). [11](#0-10) [12](#0-11)

### Citations

**File:** sync/src/relayer/block_transactions_process.rs (L45-70)
```rust
    pub async fn execute(self) -> Status {
        let shared = self.relayer.shared();
        let active_chain = shared.active_chain();
        let block_transactions = self.message.to_entity();
        let block_hash = block_transactions.block_hash();
        let received_transactions: Vec<core::TransactionView> = block_transactions
            .transactions()
            .into_iter()
            .map(|tx| tx.into_view())
            .collect();
        let received_uncles: Vec<core::UncleBlockView> = block_transactions
            .uncles()
            .into_iter()
            .map(|uncle| uncle.into_view())
            .collect();

        let mut missing_transactions: Vec<u32>;
        let mut missing_uncles: Vec<u32>;
        let mut collision = false;

        if let Entry::Occupied(mut pending) = shared
            .state()
            .pending_compact_blocks()
            .await
            .entry(block_hash.clone())
        {
```

**File:** sync/src/relayer/get_block_transactions_process.rs (L33-51)
```rust
    pub async fn execute(self) -> Status {
        let shared = self.relayer.shared();
        {
            let get_block_transactions = self.message;
            if get_block_transactions.indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "Indexes count({}) > MAX_RELAY_TXS_NUM_PER_BATCH({})",
                    get_block_transactions.indexes().len(),
                    MAX_RELAY_TXS_NUM_PER_BATCH,
                ));
            }
            if get_block_transactions.uncle_indexes().len() > shared.consensus().max_uncles_num() {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "UncleIndexes count({}) > consensus max_uncles_num({})",
                    get_block_transactions.uncle_indexes().len(),
                    shared.consensus().max_uncles_num(),
                ));
            }
        }
```

**File:** sync/src/relayer/transaction_hashes_process.rs (L25-36)
```rust
    pub fn execute(self) -> Status {
        let state = self.relayer.shared().state();
        {
            let relay_transaction_hashes = self.message;
            if relay_transaction_hashes.tx_hashes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "TxHashes count({}) > MAX_RELAY_TXS_NUM_PER_BATCH({})",
                    relay_transaction_hashes.tx_hashes().len(),
                    MAX_RELAY_TXS_NUM_PER_BATCH,
                ));
            }
        }
```

**File:** sync/src/relayer/block_proposal_process.rs (L25-37)
```rust
        {
            let block_proposals = self.message;
            let limit = shared.consensus().max_block_proposals_limit()
                * (shared.consensus().max_uncles_num() as u64);
            if (block_proposals.transactions().len() as u64) > limit {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "Transactions count({}) > consensus max_block_proposals_limit({}) * max_uncles_num({})",
                    block_proposals.transactions().len(),
                    shared.consensus().max_block_proposals_limit(),
                    shared.consensus().max_uncles_num(),
                ));
            }
        }
```

**File:** sync/src/relayer/mod.rs (L59-61)
```rust
pub const MAX_RELAY_PEERS: usize = 128;
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
pub const MAX_RELAY_TXS_BYTES_PER_BATCH: usize = 1024 * 1024;
```

**File:** sync/src/relayer/mod.rs (L106-123)
```rust
    async fn try_process(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        message: packed::RelayMessageUnionReader<'_>,
    ) -> Status {
        // CompactBlock will be verified by POW, it's OK to skip rate limit checking.
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

**File:** sync/src/relayer/mod.rs (L156-165)
```rust
            packed::RelayMessageUnionReader::BlockTransactions(reader) => {
                if reader.check_data() {
                    BlockTransactionsProcess::new(reader, self, nc, peer)
                        .execute()
                        .await
                } else {
                    StatusCode::ProtocolMessageIsMalformed
                        .with_context("BlockTransactions is invalid")
                }
            }
```

**File:** sync/src/relayer/mod.rs (L195-204)
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

**File:** rpc/src/server.rs (L164-166)
```rust
        handler.spawn(async move {
            let codec = LinesCodec::new_with_max_length(2 * 1024 * 1024);
            let stream_config = StreamServerConfig::default()
```
