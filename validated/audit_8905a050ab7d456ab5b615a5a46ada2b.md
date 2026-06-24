Audit Report

## Title
Missing Duplicate `tx_hashes` Deduplication in `GetTransactionsProofProcess::execute` Causes Panic via `.expect()` on `None` from `CBMT::build_merkle_proof` — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

## Summary

`GetTransactionsProofProcess::execute` validates `tx_hashes` only for emptiness and count, but not for duplicates. Sending `tx_hashes = [h, h]` for any known committed transaction causes duplicate leaf indices to be passed to `CBMT::build_merkle_proof`, which returns `None` for duplicate indices, triggering an unconditional `.expect()` panic. This is reachable by any unprivileged peer connected via the light-client P2P protocol and constitutes a remotely-triggerable node crash.

## Finding Description

The `execute` method in `get_transactions_proof.rs` performs two input checks — empty and over-limit — but no deduplication: [1](#0-0) 

The `partition` call at L54–64 preserves duplicates. Both copies of `h` satisfy the predicate (the tx is on-chain), so both land in `found`: [2](#0-1) 

Both copies are then pushed into `txs_in_blocks[block_hash]` with the same `tx_info.index = I`, producing `[(tx, I), (tx, I)]`: [3](#0-2) 

`CBMT::build_merkle_proof` is then called with indices `[I, I]`. The `merkle_cbt` library rejects duplicate leaf indices and returns `None`. The unconditional `.expect()` panics: [4](#0-3) 

The `received` handler calls `try_process` → `execute` inline (no `tokio::spawn` isolation), so the panic propagates directly: [5](#0-4) [6](#0-5) 

**Contrast with `GetBlocksProofProcess`**, which explicitly guards against this with a `HashSet` deduplication check before any processing: [7](#0-6) 

The RPC path (`get_tx_indices`) also explicitly uses a `HashSet` and returns an error on duplicate indices: [8](#0-7) 

The `LightClientProtocol` is registered as a production protocol: [9](#0-8) 

## Impact Explanation

The panic in the async handler crashes the light-client protocol service task. Because `received` awaits `try_process` directly without task isolation, the panic propagates and terminates the handler, causing a denial-of-service against the node's light-client service. This matches the **High** impact class: "Vulnerabilities which could easily crash a CKB node."

## Likelihood Explanation

Any unprivileged peer connected via the light-client P2P protocol can trigger this. The attacker only needs one committed transaction hash (trivially obtained from any block explorer or by observing the chain). Sending `tx_hashes = [h, h]` is a single crafted P2P message requiring no special privileges, no PoW, and no key material. The attack is repeatable and requires no victim interaction.

## Recommendation

Add a duplicate-hash check at the top of `execute`, mirroring the pattern in `GetBlocksProofProcess`:

```rust
let mut uniq = HashSet::new();
if !self.message.tx_hashes().to_entity().into_iter().all(|h| uniq.insert(h)) {
    return StatusCode::MalformedProtocolMessage.with_context("duplicate tx hash exists");
}
```

Alternatively, use a `HashMap<tx_hash, (tx, index)>` instead of a `Vec` when building `txs_in_blocks`, which naturally deduplicates by hash and prevents duplicate indices from reaching `CBMT::build_merkle_proof`.

## Proof of Concept

1. Start a CKB node with `LightClient` protocol enabled.
2. Mine a block containing a non-coinbase transaction with hash `h`.
3. Connect as a light-client peer and send:
   ```
   GetTransactionsProof {
     last_hash: <current tip hash>,
     tx_hashes: [h, h]
   }
   ```
4. Observe the node panics at `get_transactions_proof.rs:97` with message `"build proof with verified inputs should be OK"`.

Unit test equivalent: construct a `MockChain`, commit a transaction, send `GetTransactionsProof` with `tx_hashes = [tx.hash(), tx.hash()]`, and assert no panic occurs and a valid or error `Status` is returned (currently it panics before returning).

### Citations

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

**File:** util/light-client-protocol-server/src/lib.rs (L79-92)
```rust
        let item_name = msg.item_name();
        let status = self.try_process(&nc, peer, msg).await;
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
    }
```

**File:** util/light-client-protocol-server/src/lib.rs (L118-122)
```rust
            packed::LightClientMessageUnionReader::GetTransactionsProof(reader) => {
                components::GetTransactionsProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
```

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

**File:** util/launcher/src/lib.rs (L467-474)
```rust
        if support_protocols.contains(&SupportProtocol::LightClient) {
            let light_client = LightClientProtocol::new(shared.clone());
            protocols.push(CKBProtocol::new_with_support_protocol(
                SupportProtocols::LightClient,
                Box::new(light_client),
                Arc::clone(&network_state),
            ));
        } else {
```
