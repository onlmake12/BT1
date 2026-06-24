All cited code is confirmed accurate against the actual repository. Here is the validated report:

---

Audit Report

## Title
Missing Duplicate Index Validation in `GetBlockTransactionsProcess` Enables Bandwidth Amplification - (File: `sync/src/relayer/get_block_transactions_process.rs`)

## Summary
`GetBlockTransactionsProcess::execute()` bounds-checks the length of the `indexes` field against `MAX_RELAY_TXS_NUM_PER_BATCH` (32,767) but never checks for duplicate values. An unprivileged P2P peer can send a single `GetBlockTransactions` message with up to 32,767 identical index values, forcing the responding node to fetch, clone, and serialize the same transaction thousands of times in one `BlockTransactions` reply, achieving significant bandwidth amplification.

## Finding Description
In `sync/src/relayer/get_block_transactions_process.rs`, `execute()` performs two guard checks: [1](#0-0) 

Neither check tests for duplicate values within `indexes`. The handler then unconditionally iterates over every element — including repeated ones — fetching and cloning the corresponding transaction for each entry: [2](#0-1) 

All collected transactions (including duplicates) are packed into a `BlockTransactions` response and sent back: [3](#0-2) 

By contrast, both analogous handlers explicitly deduplicate before processing. `GetBlockProposalProcess` uses a `HashSet` and rejects if sizes differ: [4](#0-3) 

`GetTransactionsProcess` applies the same pattern: [5](#0-4) 

`GetBlockTransactionsProcess` is the only relay message handler that accepts a variable-length list of identifiers without a uniqueness check. `MAX_RELAY_TXS_NUM_PER_BATCH` is confirmed to be 32,767: [6](#0-5) 

## Impact Explanation
A single malicious peer sends one `GetBlockTransactions` message with `indexes = [0, 0, 0, …, 0]` (32,767 copies of index 0). The victim node fetches, clones, and serializes the coinbase transaction 32,767 times into one response. For a 200-byte transaction this produces a ~6.5 MB response from a ~131 KB request — roughly a 50× amplification factor. Multiple attacker peers compound the effect linearly. This matches the **High** impact class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation
The attack requires only a standard P2P connection — no privileged role, key material, or majority hashpower. The attacker only needs any valid `block_hash` (trivially obtained from the public chain) and any valid transaction index within that block. Index 0 (the coinbase) is always present in every block, making this trivially reproducible against any synced CKB node. The `GetBlockTransactions` message is a normal part of the compact block relay protocol, so sending it raises no suspicion and bypasses no authentication layer.

## Recommendation
Add a duplicate-index check in `GetBlockTransactionsProcess::execute()` immediately after the existing count check, mirroring the pattern in `GetBlockProposalProcess` and `GetTransactionsProcess`:

```rust
// After the existing count check at line 43:
let indexes_set: HashSet<u32> = get_block_transactions
    .indexes()
    .iter()
    .map(Into::<u32>::into)
    .collect();
if indexes_set.len() != get_block_transactions.indexes().len() {
    return StatusCode::RequestDuplicate
        .with_context("Duplicate transaction indexes");
}
```

Apply the same deduplication check to `uncle_indexes` for consistency.

## Proof of Concept
```
Attacker → Victim (GetBlockTransactions):
  block_hash:    <any valid stored block hash, e.g. chain tip>
  indexes:       [0, 0, 0, …, 0]  (32,767 entries, all index 0 / coinbase)
  uncle_indexes: []

Victim → Attacker (BlockTransactions):
  block_hash:    <same hash>
  transactions:  [coinbase_tx, coinbase_tx, …]  (32,767 copies)
  uncles:        []
```

The loop at lines 61–71 of `get_block_transactions_process.rs` executes 32,767 times for the same transaction, producing a response up to ~50× larger than the request. No special privileges or chain state manipulation are required. Index 0 (coinbase) is valid in every block, making this trivially reproducible against any synced CKB node.

### Citations

**File:** sync/src/relayer/get_block_transactions_process.rs (L37-50)
```rust
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

**File:** sync/src/relayer/get_block_proposal_process.rs (L47-52)
```rust
        let proposals: HashSet<packed::ProposalShortId> =
            self.message.proposals().to_entity().into_iter().collect();

        if proposals.len() != message_len {
            return StatusCode::RequestDuplicate.with_context("Request duplicate proposal");
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

**File:** util/constant/src/sync.rs (L68-68)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
```
