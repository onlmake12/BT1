### Title
Missing Duplicate Index Check in `GetBlockTransactionsProcess` Allows Bandwidth Amplification — (`File: sync/src/relayer/get_block_transactions_process.rs`)

---

### Summary

`GetBlockTransactionsProcess::execute()` accepts `GetBlockTransactions` relay messages from any peer and responds with the requested block transactions. It validates the *count* of `indexes` and `uncle_indexes` against configured limits, but never checks for **duplicate values** within those lists. An unprivileged peer can send a single message with `MAX_RELAY_TXS_NUM_PER_BATCH` identical indexes, causing the node to serialize and transmit the same transaction that many times in one `BlockTransactions` response — a direct bandwidth and CPU amplification primitive.

---

### Finding Description

In `GetBlockTransactionsProcess::execute()`, the handler iterates over the raw `indexes` list with `filter_map`, collecting one `TransactionView` per index entry without any deduplication:

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
``` [1](#0-0) 

The same pattern applies to `uncle_indexes`:

```rust
let uncles = self
    .message
    .uncle_indexes()
    .iter()
    .filter_map(|i| block.uncles().get(Into::<u32>::into(i) as usize))
    .collect::<Vec<_>>();
``` [2](#0-1) 

The only guards present are a count ceiling against `MAX_RELAY_TXS_NUM_PER_BATCH` for `indexes` and `max_uncles_num` for `uncle_indexes`: [3](#0-2) 

No uniqueness check is performed. Every other analogous relay handler in the codebase does perform such a check:

- `GetTransactionsProcess` deduplicates via `ProposalShortId` set and rejects if `message_len != tx_hashes_set.len()`: [4](#0-3) 

- `GetBlockProposalProcess` converts to a `HashSet` and rejects if `proposals.len() != message_len`: [5](#0-4) 

- `GetBlocksProcess` uses a `dedup` `HashSet` and returns `StatusCode::RequestDuplicate` on collision: [6](#0-5) 

`GetBlockTransactionsProcess` is the sole handler that omits this guard, making it the structural analog of the reported missing-uniqueness-check class.

---

### Impact Explanation

An attacker fills `indexes` with `MAX_RELAY_TXS_NUM_PER_BATCH` copies of the same valid transaction index (e.g., `[1, 1, 1, …, 1]`). The node:

1. Clones the same `TransactionView` `MAX_RELAY_TXS_NUM_PER_BATCH` times into a `Vec`.
2. Serializes all copies into a single `BlockTransactions` message.
3. Transmits the oversized message back to the attacker.

The attacker's request is a fixed-size message; the response is up to `MAX_RELAY_TXS_NUM_PER_BATCH × sizeof(tx)` bytes. For a large transaction this is a significant amplification ratio achievable with a single small packet. Repeated from multiple connections this constitutes a bandwidth-exhaustion DoS against the node's relay service. The same amplification applies to `uncle_indexes` up to `max_uncles_num` copies.

**Impact: Medium** — no consensus or fund-loss impact, but a reachable, repeatable bandwidth/CPU amplification primitive against any listening CKB relay node.

---

### Likelihood Explanation

**Likelihood: High.** The `GetBlockTransactions` message is part of the public relay protocol (`RelayV3`). Any peer that has completed the handshake — no authentication, no stake, no special role — can send it. The attacker only needs to know one valid `block_hash` (trivially obtained from any block explorer or `get_tip_block_hash` RPC) and one valid transaction index within that block (index 0, the cellbase, always exists). No hash collision, no key material, and no privileged access is required.

---

### Recommendation

Add a duplicate-index check at the top of `GetBlockTransactionsProcess::execute()`, consistent with every other relay handler:

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

---

### Proof of Concept

1. Connect to any CKB mainnet/testnet node as a relay peer (complete the `RelayV3` handshake).
2. Obtain any committed block hash `H` and confirm it has at least one non-cellbase transaction at index `1`.
3. Craft a `GetBlockTransactions` message:
   - `block_hash = H`
   - `indexes = [1, 1, 1, …, 1]` repeated `MAX_RELAY_TXS_NUM_PER_BATCH` times
   - `uncle_indexes = []`
4. Send the message to the node.
5. Observe the node's `BlockTransactions` response: it will contain `MAX_RELAY_TXS_NUM_PER_BATCH` identical copies of the transaction at index 1, each fully serialized.

The response size is `MAX_RELAY_TXS_NUM_PER_BATCH × tx_serialized_size`, while the request size is constant. Repeating this in a loop from multiple connections amplifies outbound bandwidth consumption on the victim node proportionally.

The root cause is confirmed at: [7](#0-6)

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
