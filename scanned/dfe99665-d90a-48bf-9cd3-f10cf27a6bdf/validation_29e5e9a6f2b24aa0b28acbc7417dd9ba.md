### Title
Duplicate tx_hash Amplification in GetTransactionsProof — No Deduplication Before DB Access - ([File: util/light-client-protocol-server/src/components/get_transactions_proof.rs])

### Summary

An unprivileged remote peer can send a `GetTransactionsProof` message containing 1000 copies of the same `tx_hash`. Because there is no deduplication before the partition loop or the `found` accumulation loop, the server performs 2000 redundant DB reads (1000× `get_transaction_info` + 1000× `get_transaction_with_info`) for what is logically a single-item request. Additionally, `CBMT::build_merkle_proof` is invoked with 1000 duplicate indices, which may cause a panic via `.expect()`.

### Finding Description

`GET_TRANSACTIONS_PROOF_LIMIT` is set to 1000. [1](#0-0) 

The only guard before processing is an upper-bound check on the raw length of `tx_hashes`. A message with 1000 identical hashes passes this check. [2](#0-1) 

The partition loop at lines 54–64 iterates over every element of `tx_hashes` without deduplication, calling `snapshot.get_transaction_info(tx_hash)` and `snapshot.is_main_chain()` for each one. [3](#0-2) 

The `found` accumulation loop at lines 67–75 then calls `snapshot.get_transaction_with_info(&tx_hash)` for every element in `found` — again without deduplication — pushing 1000 identical `(tx, index)` pairs into the same HashMap entry. [4](#0-3) 

Finally, `CBMT::build_merkle_proof` is called with a `Vec` of 1000 duplicate indices, and the result is unwrapped with `.expect("build proof with verified inputs should be OK")`. If the CBMT implementation rejects or panics on duplicate indices, this crashes the handler. [5](#0-4) 

### Impact Explanation

Per malicious message: **2000 DB reads** (1000 `get_transaction_info` + 1000 `get_transaction_with_info`) instead of 2. This is a **1000× amplification** of DB I/O from a single P2P message. Sustained at even modest message rates this saturates the RocksDB read path. The secondary risk is a node crash if `build_merkle_proof` panics on duplicate indices. Impact: **Low (501–2000 redundant DB reads per message)**, matching the stated scope.

### Likelihood Explanation

The attacker needs only a valid tx hash on the main chain (publicly observable) and the ability to connect as a light-client peer (no authentication required). The exploit is trivially constructable and repeatable.

### Recommendation

Deduplicate `tx_hashes` immediately after the length check, before any DB access:

```rust
let mut seen = std::collections::HashSet::new();
let tx_hashes: Vec<_> = self.message.tx_hashes().to_entity()
    .into_iter()
    .filter(|h| seen.insert(h.clone()))
    .collect();
if tx_hashes.is_empty() { ... }
if tx_hashes.len() > GET_TRANSACTIONS_PROOF_LIMIT { ... }
```

This reduces DB calls to at most `GET_TRANSACTIONS_PROOF_LIMIT` unique hashes and eliminates the duplicate-index risk in `build_merkle_proof`.

### Proof of Concept

1. Obtain any valid on-chain tx hash `H`.
2. Construct a `GetTransactionsProof` message with `tx_hashes = [H; 1000]` and a valid `last_hash`.
3. Send to a light-client protocol server peer.
4. Instrument `get_transaction_info`: assert it is called **1000 times** rather than 1.
5. Observe 2000 total DB reads for a logically single-item request. [6](#0-5)

### Citations

**File:** util/light-client-protocol-server/src/constant.rs (L7-7)
```rust
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L37-39)
```rust
        if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
        }
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L54-75)
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
