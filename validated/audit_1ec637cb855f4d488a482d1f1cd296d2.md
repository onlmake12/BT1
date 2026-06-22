### Title
Missing Duplicate Index Check in `GetBlockTransactions` Handler Allows Bandwidth Amplification - (File: `sync/src/relayer/get_block_transactions_process.rs`)

### Summary
The `GetBlockTransactionsProcess` handler accepts `indexes` and `uncle_indexes` arrays from any peer without checking for duplicate entries. An attacker can send a single `GetBlockTransactions` message with all `MAX_RELAY_TXS_NUM_PER_BATCH` slots pointing to the same large transaction, causing the serving node to serialize and transmit that transaction `MAX_RELAY_TXS_NUM_PER_BATCH` times in one response. Every other analogous relay handler in the codebase already performs this deduplication check.

### Finding Description

`GetBlockTransactionsProcess::execute()` validates only the *count* of `indexes` and `uncle_indexes`, not their uniqueness: [1](#0-0) 

After passing the count gate, the handler iterates over the raw (potentially duplicate) index list and collects one transaction per index entry: [2](#0-1) 

If an attacker sends `MAX_RELAY_TXS_NUM_PER_BATCH` identical indices all pointing to the largest transaction in the block, the node serializes and sends that transaction `MAX_RELAY_TXS_NUM_PER_BATCH` times in the `BlockTransactions` response.

**Contrast with every other relay handler that already performs this check:**

`GetTransactionsProcess` (handling `GetRelayTransactions`) explicitly deduplicates `tx_hashes` via a `HashSet` and returns `StatusCode::RequestDuplicate` on collision: [3](#0-2) 

`GetBlockProposalProcess` (handling `GetBlockProposal`) does the same for `proposals`: [4](#0-3) 

`GetBlocksProcess` (handling `GetBlocks`) uses a `dedup` `HashSet` for `block_hashes`: [5](#0-4) 

`GetBlockTransactionsProcess` is the only relay request handler that omits this guard.

The message schema confirms `indexes` is an unconstrained `Uint32Vec`: [6](#0-5) 

### Impact Explanation

An unprivileged peer can force the serving node to:
1. Perform `MAX_RELAY_TXS_NUM_PER_BATCH` redundant block-transaction lookups.
2. Serialize and transmit `MAX_RELAY_TXS_NUM_PER_BATCH` copies of the same (potentially large) transaction in a single response, amplifying outbound bandwidth consumption relative to the inbound request size.

The rate limiter caps this at 30 relay messages per second per peer, but within each message the attacker maximizes the per-response payload by choosing the largest transaction in the block. Multiple peers can compound the effect without requiring a Sybil majority.

### Likelihood Explanation

Any peer that has received a compact block and triggered the `GetBlockTransactions` flow can craft this message. No special privilege, key, or hashpower is required. The message passes all existing validation (count ≤ `MAX_RELAY_TXS_NUM_PER_BATCH`, `uncle_indexes` ≤ `max_uncles_num`) and reaches the response-building path unconditionally.

### Recommendation

Add a deduplication check for both `indexes` and `uncle_indexes` immediately after the count checks, consistent with the pattern already used in `GetTransactionsProcess` and `GetBlockProposalProcess`:

```rust
// After the existing count checks:
let indexes = get_block_transactions.indexes();
let indexes_set: HashSet<u32> = indexes.iter().map(Into::into).collect();
if indexes_set.len() != indexes.len() {
    return StatusCode::RequestDuplicate.with_context("Duplicate transaction indexes");
}

let uncle_indexes = get_block_transactions.uncle_indexes();
let uncle_indexes_set: HashSet<u32> = uncle_indexes.iter().map(Into::into).collect();
if uncle_indexes_set.len() != uncle_indexes.len() {
    return StatusCode::RequestDuplicate.with_context("Duplicate uncle indexes");
}
```

### Proof of Concept

1. Connect to a CKB node as a relay peer.
2. Obtain a block hash for a block that contains a large transaction at index `k`.
3. Send a `GetBlockTransactions` message:
   - `block_hash`: the target block hash
   - `indexes`: `[k, k, k, …]` repeated `MAX_RELAY_TXS_NUM_PER_BATCH` times
   - `uncle_indexes`: `[]`
4. Observe that the node responds with a `BlockTransactions` message containing `MAX_RELAY_TXS_NUM_PER_BATCH` copies of the same transaction, with outbound bytes ≈ `MAX_RELAY_TXS_NUM_PER_BATCH × tx_size` for an inbound request of only a few dozen bytes.

The root cause is the absence of a uniqueness check in `GetBlockTransactionsProcess::execute()` at `sync/src/relayer/get_block_transactions_process.rs` lines 37–50, while all peer relay handlers for analogous request types already enforce this invariant. [7](#0-6)

### Citations

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

**File:** util/gen-types/schemas/extensions.mol (L173-177)
```text
table GetBlockTransactions {
    block_hash:                 Byte32,
    indexes:                    Uint32Vec,
    uncle_indexes:              Uint32Vec,
}
```
