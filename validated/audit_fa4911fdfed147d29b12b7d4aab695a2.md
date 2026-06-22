Based on my analysis of the actual code, here is my assessment:

---

### Title
Missing Deduplication Guard in `GetTransactionsProofProcess` Enables O(N) Work Amplification per Request — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

### Summary

`GetTransactionsProofProcess::execute` performs no deduplication on the incoming `tx_hashes` list. An unprivileged remote peer with an open LightClient session can send a `GetTransactionsProof` message containing up to 1000 identical confirmed transaction hashes, forcing the server to perform O(N) database reads, O(N) CBMT index construction, and O(N) response serialization — with no ban applied.

### Finding Description

`GetBlocksProofProcess::execute` explicitly deduplicates its input before processing: [1](#0-0) 

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

`GetTransactionsProofProcess::execute` has no equivalent guard: [2](#0-1) 

The only check is a count limit of 1000: [3](#0-2) 

With 1000 identical confirmed tx hashes, the execution path is:

1. **Partition (lines 54–64):** Iterates all 1000 hashes, calling `snapshot.get_transaction_info(tx_hash)` and `snapshot.is_main_chain(...)` for each — 1000 DB reads. [4](#0-3) 

2. **HashMap build (lines 66–75):** Calls `snapshot.get_transaction_with_info(&tx_hash)` for each of the 1000 hashes — another 1000 DB reads. All 1000 entries land in the same block's Vec as `(tx, same_index)` pairs. [5](#0-4) 

3. **CBMT proof construction (lines 86–97):** `CBMT::build_merkle_proof` is called with a Vec of 1000 identical `u32` indices. [6](#0-5) 

4. **Response serialization (lines 99–114):** A `FilteredBlock` is built containing 1000 copies of the same transaction data, then serialized and sent. [7](#0-6) 

### Impact Explanation

- **2000 DB reads** per request (1000 `get_transaction_info` + 1000 `get_transaction_with_info`) for a single small P2P message.
- **Large response**: 1000 copies of the same transaction data serialized into a `FilteredBlock`, amplifying outbound bandwidth.
- **No ban**: The peer is never banned. `MalformedProtocolMessage` is never returned for this case, so `should_ban()` is never triggered. [8](#0-7) 

An attacker can repeat this indefinitely, sustaining amplified I/O and CPU load on the server.

### Likelihood Explanation

The attack requires only an open LightClient P2P session and knowledge of one confirmed on-chain transaction hash — both trivially obtainable. No privileged access, hashpower, or key material is needed. The protocol message is well-formed and passes all existing validation.

### Recommendation

Add a deduplication check immediately after the count check, mirroring `GetBlocksProofProcess`:

```rust
let tx_hashes_vec: Vec<_> = self.message.tx_hashes().to_entity().into_iter().collect();
let mut uniq = HashSet::new();
if !tx_hashes_vec.iter().all(|h| uniq.insert(h.clone())) {
    return StatusCode::MalformedProtocolMessage
        .with_context("duplicate tx hash exists");
}
```

This would trigger a ban via `should_ban()` and eliminate the amplification. [9](#0-8) 

### Proof of Concept

1. Connect to a CKB node running the LightClient protocol.
2. Obtain any confirmed on-chain transaction hash `h`.
3. Send `GetTransactionsProof { last_hash: tip_hash, tx_hashes: [h; 1000] }`.
4. Observe the server performs 2000 DB reads and returns a response containing 1000 copies of the same transaction.
5. Repeat in a tight loop — no ban is ever applied.
6. Measure server CPU and I/O vs. a single-hash request to confirm the O(N) amplification ratio.

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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L32-39)
```rust
    pub(crate) async fn execute(self) -> Status {
        if self.message.tx_hashes().is_empty() {
            return StatusCode::MalformedProtocolMessage.with_context("no transaction");
        }

        if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L86-97)
```rust
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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L99-114)
```rust
            let txs: Vec<_> = txs_and_tx_indices
                .into_iter()
                .map(|(tx, _)| tx.data())
                .collect();

            let filtered_block = packed::FilteredBlock::new_builder()
                .header(block.header().data())
                .witnesses_root(block.calc_witnesses_root())
                .transactions(txs)
                .proof(
                    packed::MerkleProof::new_builder()
                        .indices(merkle_proof.indices().as_ref())
                        .lemmas(merkle_proof.lemmas().to_owned())
                        .build(),
                )
                .build();
```

**File:** util/light-client-protocol-server/src/constant.rs (L7-7)
```rust
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/lib.rs (L81-91)
```rust
        if let Some(ban_time) = status.should_ban() {
            error!(
                "process {} from {}; ban {:?} since result is {}",
                item_name, peer, ban_time, status
            );
            nc.ban_peer(peer, ban_time, status.to_string());
        } else if status.should_warn() {
            warn!("process {} from {}; result is {}", item_name, peer, status);
        } else if !status.is_ok() {
            debug!("process {} from {}; result is {}", item_name, peer, status);
        }
```
