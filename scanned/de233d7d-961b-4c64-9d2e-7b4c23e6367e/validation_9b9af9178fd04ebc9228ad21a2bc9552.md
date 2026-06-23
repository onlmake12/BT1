### Title
Missing Uniqueness Validation on `tx_hashes` Causes Duplicate Merkle Indices and Potential Panic in Light Client Protocol Server - (File: `util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

### Summary

`GetTransactionsProofProcess::execute()` accepts a `GetTransactionsProof` message from any peer without validating that the supplied `tx_hashes` are unique. Duplicate hashes cause the same transaction index to appear twice in the indices slice passed to `CBMT::build_merkle_proof`. The call is guarded by `.expect("build proof with verified inputs should be OK")`, which panics if `build_merkle_proof` returns `None` for duplicate indices, crashing the light client protocol server process.

### Finding Description

In `util/light-client-protocol-server/src/components/get_transactions_proof.rs`, the `execute` function validates only that the `tx_hashes` list is non-empty and within the count limit:

```rust
if self.message.tx_hashes().is_empty() {
    return StatusCode::MalformedProtocolMessage.with_context("no transaction");
}
if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
}
```

There is no check that the hashes are distinct. [1](#0-0) 

The found hashes are then iterated and pushed into a `Vec` keyed by block hash, with no deduplication:

```rust
let mut txs_in_blocks = HashMap::new();
for tx_hash in found {
    let (tx, tx_info) = snapshot.get_transaction_with_info(&tx_hash).expect("tx exists");
    txs_in_blocks
        .entry(tx_info.block_hash)
        .or_insert_with(Vec::new)
        .push((tx, tx_info.index));   // same index pushed twice for duplicate hashes
}
``` [2](#0-1) 

The resulting `txs_and_tx_indices` vector (with duplicate `tx_info.index` values) is then passed directly to `CBMT::build_merkle_proof`, whose result is unwrapped with `.expect`:

```rust
let merkle_proof = CBMT::build_merkle_proof(
    &block.transactions().iter().map(|tx| tx.hash()).collect::<Vec<_>>(),
    &txs_and_tx_indices
        .iter()
        .map(|(_, index)| *index as u32)
        .collect::<Vec<_>>(),
)
.expect("build proof with verified inputs should be OK");
``` [3](#0-2) 

The `CBMT` type is the `merkle_cbt` library's Complete Binary Merkle Tree: [4](#0-3) 

`build_merkle_proof` returns `Option<MerkleProof>` and returns `None` when the supplied indices are invalid (e.g., duplicated or out-of-range). The `.expect()` call converts a `None` return into a panic, crashing the thread/process handling the light client protocol.

By contrast, the RPC-side `get_tx_indices` helper correctly uses a `HashSet` to detect and reject duplicate transaction hashes before building any proof:

```rust
let mut tx_indices = HashSet::new();
...
if !tx_indices.insert(tx_info.index as u32) {
    return Err(RPCError::invalid_params(format!("Duplicated tx_hash {tx_hash:#x}")));
}
``` [5](#0-4) 

The light client protocol path has no equivalent guard.

### Impact Explanation

A remote peer connected to the light client protocol server can send a single `GetTransactionsProof` message containing any known committed transaction hash repeated twice. This triggers the unguarded `.expect()` on `build_merkle_proof`'s `None` return, causing a panic in the server process. Depending on the runtime configuration, this either crashes the entire node process or terminates the async task handling that peer, disrupting light client service. The attack requires no authentication, no special privilege, and no cryptographic capability — only knowledge of any committed transaction hash (publicly available from the chain).

### Likelihood Explanation

The `GetTransactionsProof` message is part of the light client P2P protocol, reachable by any peer that connects to the light client protocol endpoint. The required input (a duplicate tx_hash) is trivially constructable. The missing uniqueness check is a single-line omission with no compensating control in the light client path.

### Recommendation

Add a uniqueness check on `tx_hashes` at the start of `GetTransactionsProofProcess::execute()`, mirroring the pattern used in `get_tx_indices` on the RPC side:

```rust
let tx_hashes_set: HashSet<_> = self.message.tx_hashes().to_entity().into_iter().collect();
if tx_hashes_set.len() != self.message.tx_hashes().len() {
    return StatusCode::MalformedProtocolMessage.with_context("duplicate tx_hashes");
}
```

Alternatively, deduplicate `txs_and_tx_indices` by index before calling `build_merkle_proof`, or replace the `.expect()` with a graceful error return.

### Proof of Concept

1. Connect a peer to a CKB node's light client protocol endpoint.
2. Obtain any committed transaction hash `H` (e.g., from `get_transaction` RPC).
3. Send a `GetTransactionsProof` message with `tx_hashes = [H, H]` and a valid `last_hash` on the main chain.
4. The server finds both copies of `H` in the chain, pushes `(tx, idx)` twice into `txs_and_tx_indices`, calls `CBMT::build_merkle_proof(..., &[idx, idx])`, receives `None`, and panics at the `.expect("build proof with verified inputs should be OK")` call. [6](#0-5)

### Citations

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L32-97)
```rust
    pub(crate) async fn execute(self) -> Status {
        if self.message.tx_hashes().is_empty() {
            return StatusCode::MalformedProtocolMessage.with_context("no transaction");
        }

        if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
        }

        let snapshot = self.protocol.shared.snapshot();

        let last_block_hash = self.message.last_hash().to_entity();
        if !snapshot.is_main_chain(&last_block_hash) {
            return self
                .protocol
                .reply_tip_state::<packed::SendTransactionsProof>(self.peer, self.nc)
                .await;
        }
        let last_block = snapshot
            .get_block(&last_block_hash)
            .expect("block should be in store");

        let (found, missing): (Vec<_>, Vec<_>) = self
            .message
            .tx_hashes()
            .to_entity()
            .into_iter()
            .partition(|tx_hash| {
                snapshot
                    .get_transaction_info(tx_hash)
                    .map(|tx_info| snapshot.is_main_chain(&tx_info.block_hash))
                    .unwrap_or_default()
            });

        let mut txs_in_blocks = HashMap::new();
        for tx_hash in found {
            let (tx, tx_info) = snapshot
                .get_transaction_with_info(&tx_hash)
                .expect("tx exists");
            txs_in_blocks
                .entry(tx_info.block_hash)
                .or_insert_with(Vec::new)
                .push((tx, tx_info.index));
        }

        let mut positions = Vec::with_capacity(txs_in_blocks.len());
        let mut filtered_blocks = Vec::with_capacity(txs_in_blocks.len());
        let mut uncles_hash = Vec::with_capacity(txs_in_blocks.len());
        let mut extensions = Vec::with_capacity(txs_in_blocks.len());

        for (block_hash, txs_and_tx_indices) in txs_in_blocks.into_iter() {
            let block = snapshot
                .get_block(&block_hash)
                .expect("block should be in store");
            let merkle_proof = CBMT::build_merkle_proof(
                &block
                    .transactions()
                    .iter()
                    .map(|tx| tx.hash())
                    .collect::<Vec<_>>(),
                &txs_and_tx_indices
                    .iter()
                    .map(|(_, index)| *index as u32)
                    .collect::<Vec<_>>(),
            )
            .expect("build proof with verified inputs should be OK");
```

**File:** util/types/src/utilities/merkle_tree.rs (L22-25)
```rust
/// Complete Binary Merkle Tree specialized for `Byte32` leaves.
pub type CBMT = ExCBMT<Byte32, MergeByte32>;
/// Merkle proof for `Byte32` values.
pub type MerkleProof = ExMerkleProof<Byte32, MergeByte32>;
```

**File:** rpc/src/module/chain.rs (L2292-2308)
```rust
        let mut tx_indices = HashSet::new();
        for tx_hash in tx_hashes {
            match snapshot.get_transaction_info(&(&tx_hash).into()) {
                Some(tx_info) => {
                    if retrieved_block_hash.is_none() {
                        retrieved_block_hash = Some(tx_info.block_hash);
                    } else if Some(tx_info.block_hash) != retrieved_block_hash {
                        return Err(RPCError::invalid_params(
                            "Not all transactions found in retrieved block",
                        ));
                    }

                    if !tx_indices.insert(tx_info.index as u32) {
                        return Err(RPCError::invalid_params(format!(
                            "Duplicated tx_hash {tx_hash:#x}"
                        )));
                    }
```
