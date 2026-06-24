Audit Report

## Title
Missing Duplicate Index Check in `GetBlockTransactionsProcess::execute()` Enables Bandwidth Amplification — (`File: sync/src/relayer/get_block_transactions_process.rs`)

## Summary
`GetBlockTransactionsProcess::execute()` validates the *count* of `indexes` and `uncle_indexes` against configured limits but never checks for duplicate values within those lists. An unprivileged peer can send a single `GetBlockTransactions` message with up to `MAX_RELAY_TXS_NUM_PER_BATCH` (32,767) identical indexes, causing the node to serialize and transmit the same transaction 32,767 times in one `BlockTransactions` response. This is a direct, repeatable bandwidth amplification primitive against any CKB relay node, achievable with minimal attacker resources.

## Finding Description
In `GetBlockTransactionsProcess::execute()`, the only input guards are count-ceiling checks: [1](#0-0) 

After those checks pass, the handler iterates the raw `indexes` list with no deduplication: [2](#0-1) 

The same applies to `uncle_indexes`: [3](#0-2) 

`MAX_RELAY_TXS_NUM_PER_BATCH` is defined as **32,767**: [4](#0-3) 

Every analogous relay handler performs a uniqueness check before processing:

- `GetTransactionsProcess` builds a `HashSet` and rejects if `message_len != tx_hashes_set.len()`: [5](#0-4) 
- `GetBlockProposalProcess` converts to a `HashSet` and rejects if `proposals.len() != message_len`: [6](#0-5) 
- `GetBlocksProcess` uses a `dedup` `HashSet` and returns `StatusCode::RequestDuplicate` on collision: [7](#0-6) 

`GetBlockTransactionsProcess` is the sole handler that omits this guard. The root cause is the absence of a uniqueness check before the `filter_map` iteration at lines 61–78.

## Impact Explanation
An attacker sends one small `GetBlockTransactions` message with 32,767 copies of index `0` (the cellbase, always present). The node clones the same `TransactionView` 32,767 times, serializes all copies into a single `BlockTransactions` message, and transmits it. For a typical 597-byte transaction this yields ~19.6 MB per request; for a larger transaction the ratio is proportionally higher. Repeated from multiple connections with minimal attacker bandwidth, this exhausts the victim node's outbound bandwidth and relay capacity.

This matches the allowed **High** impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."* The attacker's cost is a fixed-size message; the victim's cost scales linearly with `MAX_RELAY_TXS_NUM_PER_BATCH × tx_size`. Targeting multiple nodes simultaneously degrades the relay layer network-wide.

## Likelihood Explanation
**High.** The `GetBlockTransactions` message is part of the public `RelayV3` protocol. Any peer that completes the standard handshake — no authentication, no stake, no special role — can send it. The attacker only needs one valid `block_hash` (trivially obtained from any block explorer or `get_tip_block_hash` RPC) and index `0` (the cellbase, which always exists). No hash collision, no key material, and no privileged access is required. The attack is repeatable in a tight loop.

## Recommendation
Add a duplicate-index check at the top of `execute()`, consistent with every other relay handler:

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
1. Connect to any CKB mainnet/testnet node as a relay peer (complete the `RelayV3` handshake).
2. Obtain any committed block hash `H` (e.g., from `get_tip_block_hash` RPC).
3. Craft a `GetBlockTransactions` message:
   - `block_hash = H`
   - `indexes = [0, 0, 0, …, 0]` repeated 32,767 times (cellbase always at index 0)
   - `uncle_indexes = []`
4. Send the message to the node.
5. Observe the `BlockTransactions` response: it will contain 32,767 identical serialized copies of the cellbase transaction.
6. Repeat in a loop from multiple connections to exhaust the node's outbound bandwidth.

The count check at line 37 passes (32,767 == `MAX_RELAY_TXS_NUM_PER_BATCH`), no uniqueness check exists, and the `filter_map` at lines 61–71 faithfully clones the same entry 32,767 times. [8](#0-7)

### Citations

**File:** sync/src/relayer/get_block_transactions_process.rs (L33-101)
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

        let block_hash = self.message.block_hash().to_entity();
        debug_target!(
            crate::LOG_TARGET_RELAY,
            "get_block_transactions {}",
            block_hash
        );

        if let Some(block) = shared.store().get_block(&block_hash) {
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

            let uncles = self
                .message
                .uncle_indexes()
                .iter()
                .filter_map(|i| block.uncles().get(Into::<u32>::into(i) as usize))
                .collect::<Vec<_>>();

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
        }

        Status::ok()
    }
```

**File:** util/constant/src/sync.rs (L68-68)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
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
