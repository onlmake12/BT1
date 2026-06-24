Audit Report

## Title
Missing Duplicate `tx_hashes` Validation Causes Remote Panic/DoS in Light Client Server — (File: `util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

## Summary

`GetTransactionsProofProcess::execute()` does not validate that the incoming `tx_hashes` list is free of duplicates, unlike the analogous `GetBlocksProofProcess::execute()` which explicitly performs this check. When duplicate tx hashes resolving to the same block are submitted, `CBMT::build_merkle_proof` is called with duplicate leaf indices, returns `None`, and the subsequent `.expect()` panics, crashing the node process. Any unprivileged peer can trigger this with a single valid on-chain tx hash repeated twice.

## Finding Description

`GetBlocksProofProcess::execute()` explicitly guards against duplicates before processing: [1](#0-0) 

No equivalent guard exists in `GetTransactionsProofProcess::execute()`. The only input validation present is an empty-list check and a length limit: [2](#0-1) 

When duplicate tx hash `H` is submitted twice, both copies pass the `partition` step into `found` because `get_transaction_info` succeeds for both: [3](#0-2) 

Both copies are then pushed into the same `HashMap` entry (keyed by `tx_info.block_hash`) with the same `tx_info.index`, producing `txs_and_tx_indices = [(tx, idx), (tx, idx)]`: [4](#0-3) 

`CBMT::build_merkle_proof` is then called with the duplicate index list `[idx, idx]`. The `merkle_cbt` crate's `build_merkle_proof` requires indices to be strictly sorted and unique; it returns `None` when they are not. The `.expect()` on line 97 then panics, terminating the process: [5](#0-4) 

`CBMT` is defined as `ExCBMT<Byte32, MergeByte32>` from the external `merkle_cbt` crate: [6](#0-5) 

## Impact Explanation

The panic is unrecoverable and terminates the node process. This constitutes a remotely-triggered crash of a CKB node, matching the **High** impact category: *"Vulnerabilities which could easily crash a CKB node"* (10001–15000 points). The crash is not isolated to a single connection or goroutine — a Rust `panic` in this async handler propagates and kills the process.

## Likelihood Explanation

The `GetTransactionsProof` message handler is reachable by any unprivileged peer via `LightClientProtocol::try_process`: [7](#0-6) 

No authentication or special privilege is required. Any peer that knows a single valid on-chain transaction hash (publicly observable in any block) can trigger the crash by sending it twice in the `tx_hashes` field. The attack is trivially constructible, repeatable, and requires no special tooling beyond a standard light client protocol message.

## Recommendation

Add a duplicate-check guard at the start of `GetTransactionsProofProcess::execute()`, mirroring the pattern already used in `GetBlocksProofProcess::execute()`:

```rust
let tx_hashes: Vec<_> = self.message.tx_hashes().to_entity().into_iter().collect();
let mut uniq = HashSet::new();
if !tx_hashes.iter().all(|hash| uniq.insert(hash)) {
    return StatusCode::MalformedProtocolMessage.with_context("duplicate tx hash exists");
}
```

This is consistent with the guard already present in `get_blocks_proof.rs` at lines 62–70. [1](#0-0) 

## Proof of Concept

1. Identify any valid on-chain transaction hash `H` (publicly visible in any block explorer or via RPC).
2. Connect to a CKB node with the light client server enabled.
3. Send a `GetTransactionsProof` message with `tx_hashes = [H, H]` and a valid `last_hash` pointing to a main-chain block.
4. The server resolves both copies of `H` to the same block and same `tx_info.index`, building `txs_and_tx_indices = [(tx, idx), (tx, idx)]`.
5. `CBMT::build_merkle_proof(leaves, &[idx, idx])` returns `None` due to duplicate indices.
6. `.expect("build proof with verified inputs should be OK")` panics → node process terminates. [5](#0-4)

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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L33-39)
```rust
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

**File:** util/types/src/utilities/merkle_tree.rs (L23-23)
```rust
pub type CBMT = ExCBMT<Byte32, MergeByte32>;
```

**File:** util/light-client-protocol-server/src/lib.rs (L118-122)
```rust
            packed::LightClientMessageUnionReader::GetTransactionsProof(reader) => {
                components::GetTransactionsProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
```
