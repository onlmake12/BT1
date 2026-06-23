The code confirms this is a real vulnerability. Here is the analysis:

---

### Title
Missing Deduplication in `GetTransactionsProofProcess::execute` Enables CPU/IO Amplification — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

### Summary
`GetTransactionsProofProcess::execute` performs no deduplication on the incoming `tx_hashes` list. An unprivileged remote peer can send a `GetTransactionsProof` message containing up to 1000 identical tx_hashes, causing the server to perform 1000 redundant database reads per request.

### Finding Description

`GetBlocksProofProcess::execute` explicitly builds a `HashSet` and rejects any message containing duplicate block hashes: [1](#0-0) 

`GetTransactionsProofProcess::execute` has no equivalent guard. The partition step iterates over the raw, undeduped `tx_hashes` and calls `snapshot.get_transaction_info` for every element: [2](#0-1) 

The subsequent loop then calls `snapshot.get_transaction_with_info` for every hash in `found`, again with no deduplication: [3](#0-2) 

The only bound is `GET_TRANSACTIONS_PROOF_LIMIT = 1000`: [4](#0-3) 

With 1000 identical valid tx_hashes in one message, the server performs:
- 1000 calls to `snapshot.get_transaction_info` (storage reads)
- 1000 calls to `snapshot.get_transaction_with_info` (storage reads)
- 1 call to `CBMT::build_merkle_proof` with 1000 duplicate indices (the `HashMap` collapses all duplicates into one block entry, but the indices vector still has 1000 entries) [5](#0-4) 

### Impact Explanation
Each malicious request causes O(2000) redundant storage I/O operations that would cost O(2) with proper deduplication. This is a per-request CPU/IO amplification factor of ~1000x, exploitable by any peer connected to the light client protocol with no authentication required.

### Likelihood Explanation
The light client protocol is reachable by any unprivileged peer. The message is structurally valid (passes the length check), and the only requirement is knowing one valid confirmed tx_hash on the main chain, which is trivially obtained from any block explorer or chain scan.

### Recommendation
Add a deduplication check immediately after the length check, mirroring the pattern in `GetBlocksProofProcess`:

```rust
let tx_hashes: Vec<_> = self.message.tx_hashes().to_entity().into_iter().collect();
let mut uniq = HashSet::new();
if !tx_hashes.iter().all(|h| uniq.insert(h)) {
    return StatusCode::MalformedProtocolMessage.with_context("duplicate tx hash exists");
}
```

### Proof of Concept
1. Connect to a CKB node with the light client protocol enabled.
2. Obtain any valid confirmed tx_hash `H` from the main chain.
3. Send a `GetTransactionsProof` message with `last_hash` = current tip, `tx_hashes` = `[H; 1000]`.
4. Instrument `get_transaction_info` call count on the server; assert it is called 1000 times, not 1.
5. Repeat in a tight loop to exhaust server I/O capacity.

### Citations

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L62-70)
```rust
        let mut uniq = HashSet::new();
        if !block_hashes
            .iter()
            .chain([last_block_hash].iter())
            .all(|hash| uniq.insert(hash))
        {
            return StatusCode::MalformedProtocolMessage
                .with_context("duplicate block hash exists");
        }
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L54-64)
```rust
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
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L66-75)
```rust
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
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L82-97)
```rust
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

**File:** util/light-client-protocol-server/src/constant.rs (L7-7)
```rust
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```
