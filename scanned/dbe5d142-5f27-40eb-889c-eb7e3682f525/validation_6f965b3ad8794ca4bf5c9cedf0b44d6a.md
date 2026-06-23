### Title
Missing Deduplication of `tx_hashes` Causes 2×N Redundant DB Lookups and Duplicate Proof Entries — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

---

### Summary

`GetTransactionsProofProcess::execute` accepts up to `GET_TRANSACTIONS_PROOF_LIMIT` (1000) `tx_hashes` from any unprivileged light-client peer without deduplicating them. When N identical hashes for a committed on-chain transaction are submitted, the handler performs 2×N redundant DB reads and accumulates N duplicate `(tx, index)` entries into `txs_in_blocks`, then feeds those duplicate indices into `CBMT::build_merkle_proof`.

---

### Finding Description

The only input guard is a length check: [1](#0-0) 

No deduplication is applied before the two-stage processing.

**Stage 1 — partition:** `get_transaction_info` is called once per element in the raw (possibly duplicate-filled) iterator: [2](#0-1) 

**Stage 2 — found loop:** `get_transaction_with_info` is called once per element in `found`, again without deduplication: [3](#0-2) 

For N identical hashes that resolve to the same committed transaction, this produces:
- N calls to `get_transaction_info` (Stage 1)
- N calls to `get_transaction_with_info` (Stage 2)
- N identical `(tx, tx_info.index)` entries pushed into `txs_in_blocks[block_hash]`

Those N duplicate entries are then passed directly to `CBMT::build_merkle_proof`: [4](#0-3) 

The `.expect("build proof with verified inputs should be OK")` at line 97 assumes valid (non-duplicate) indices. Feeding 1000 copies of the same index may cause the proof builder to return an error, which `.expect()` converts to a panic — a hard crash of the handler task.

The constant ceiling is: [5](#0-4) 

---

### Impact Explanation

A single malicious peer can send one `GetTransactionsProof` message containing 1000 copies of the same valid on-chain `tx_hash`, forcing the server to:

1. Execute 2000 RocksDB point-reads (2× the limit) for a single unique transaction.
2. Allocate and process a `Vec` of 1000 identical `(tx, index)` tuples.
3. Invoke `CBMT::build_merkle_proof` with 1000 duplicate indices, which is either computationally expensive or triggers a panic via `.expect()`.
4. Serialize and transmit a `FilteredBlock` containing 1000 duplicate transaction entries.

Multiple concurrent connections each sending such a message multiply the amplification linearly. The impact is server-side CPU and I/O amplification per request, matching the Low (501–2000) scope.

---

### Likelihood Explanation

The attack requires no privilege, no key material, and no PoW. Any peer that can open a light-client protocol connection can send this message. The `GetTransactionsProof` message type is part of the standard `LightClientMessage` union and is reachable through the normal P2P receive path. [6](#0-5) 

---

### Recommendation

Deduplicate `tx_hashes` immediately after the length check, before any DB access:

```rust
// After the length check at line 39:
let mut seen = std::collections::HashSet::new();
let tx_hashes: Vec<_> = self
    .message
    .tx_hashes()
    .to_entity()
    .into_iter()
    .filter(|h| seen.insert(h.clone()))
    .collect();
```

Then operate on this deduplicated `tx_hashes` vec for both the partition and the found loop. This reduces DB lookups to at most N unique hashes and eliminates duplicate entries in `txs_in_blocks`.

---

### Proof of Concept

```rust
// Pseudocode unit test
let committed_tx_hash = /* hash of a tx committed on main chain */;
let tx_hashes = vec![committed_tx_hash; 1000]; // 1000 identical hashes

let msg = packed::GetTransactionsProof::new_builder()
    .last_hash(snapshot.tip_header().hash())
    .tx_hashes(tx_hashes.pack())
    .build();

// Assert: get_transaction_info called 1000 times (not 1)
// Assert: get_transaction_with_info called 1000 times (not 1)
// Assert: txs_in_blocks[block_hash].len() == 1000 (not 1)
// Assert: CBMT::build_merkle_proof receives 1000 duplicate indices
//         → either panics or produces a 1000-entry proof for one tx
```

### Citations

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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L67-75)
```rust
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

**File:** util/light-client-protocol-server/src/constant.rs (L7-7)
```rust
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```
