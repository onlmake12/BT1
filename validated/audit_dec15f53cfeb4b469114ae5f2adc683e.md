All cited code references check out against the actual repository. The vulnerability is confirmed:

1. `execute()` only checks for empty/over-limit `tx_hashes` — no deduplication guard. [1](#0-0) 
2. The `partition` call preserves duplicates in `found`. [2](#0-1) 
3. Both copies of the same hash push `(tx, I)` into the same `Vec` in `txs_in_blocks`, producing duplicate index `I`. [3](#0-2) 
4. `CBMT::build_merkle_proof` is called with `[I, I]` and returns `None`; `.expect()` panics. [4](#0-3) 
5. `CBMT` is `ExCBMT<Byte32, MergeByte32>` from `merkle_cbt` v0.3.2, which returns `None` for duplicate indices. [5](#0-4) 
6. `GetBlocksProofProcess` has the analogous `HashSet` guard; `GetTransactionsProofProcess` does not. [6](#0-5) 
7. The RPC path also guards with a `HashSet` and returns an error on duplicate indices. [7](#0-6) 
8. `received` → `try_process` → `execute` has no panic isolation. [8](#0-7) 
9. `LightClientProtocol` is registered as a production protocol. [9](#0-8) 

---

Audit Report

## Title
Missing Deduplication of `tx_hashes` in `GetTransactionsProofProcess::execute` Causes Panic via `.expect()` on `None` from `CBMT::build_merkle_proof` — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

## Summary

`GetTransactionsProofProcess::execute` does not deduplicate `tx_hashes` before building the CBMT Merkle proof. Sending `tx_hashes = [h, h]` for any committed transaction causes duplicate leaf indices to be passed to `CBMT::build_merkle_proof`, which returns `None`, triggering an unconditional `.expect()` panic at line 97. This is reachable by any unprivileged peer connected to the light-client P2P protocol and causes a denial-of-service against the node's light-client protocol handler.

## Finding Description

`execute` only validates that `tx_hashes` is non-empty and within the count limit; there is no deduplication guard:

```rust
// get_transactions_proof.rs L33-39
if self.message.tx_hashes().is_empty() {
    return StatusCode::MalformedProtocolMessage.with_context("no transaction");
}
if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
}
```

The `partition` call at L54–64 preserves duplicates: both copies of `h` land in `found` because the same transaction exists on-chain. The loop at L66–75 then pushes both `(tx, I)` entries into the same `Vec` under the same `block_hash` key in `txs_in_blocks`, producing `[(tx, I), (tx, I)]` with the same index `I`. At L86–97, `CBMT::build_merkle_proof` is called with indices `[I, I]`. The `merkle_cbt` v0.3.2 library rejects duplicate leaf indices and returns `None`. The unconditional `.expect("build proof with verified inputs should be OK")` panics.

By contrast, `GetBlocksProofProcess::execute` explicitly guards against duplicates with a `HashSet` before any processing (L62–70 of `get_blocks_proof.rs`), and the RPC `get_tx_indices` path also uses a `HashSet` and returns an error on duplicate indices (`chain.rs` L2292–2308). The `GetTransactionsProofProcess` path has no equivalent guard.

The panic propagates through `try_process` → `received` in `lib.rs` with no recovery, crashing the async task for `LightClientProtocol`.

## Impact Explanation

A panic in the `LightClientProtocol` async handler crashes the protocol service task, causing a denial-of-service against all light-client peers or the entire node process. `LightClientProtocol` is registered as a production protocol in `util/launcher/src/lib.rs`. This matches the **High** impact class: *Vulnerabilities which could easily crash a CKB node* (10001–15000 points).

## Likelihood Explanation

Any unprivileged peer connected via the light-client P2P protocol can trigger this. The attacker needs only one committed transaction hash, trivially obtainable from any block explorer or by observing the chain. Sending `tx_hashes = [h, h]` is a single crafted P2P message requiring no special privileges, no PoW, and no key material. The attack is repeatable and requires no victim interaction.

## Recommendation

Add a duplicate-hash check at the top of `execute`, mirroring the pattern in `GetBlocksProofProcess`:

```rust
let mut uniq = HashSet::new();
if !self.message.tx_hashes().to_entity().into_iter().all(|h| uniq.insert(h)) {
    return StatusCode::MalformedProtocolMessage.with_context("duplicate tx hash exists");
}
```

Alternatively, replace the `Vec` in `txs_in_blocks` with a `HashMap<Byte32, (TransactionView, usize)>` keyed by `tx_hash`, which naturally deduplicates by hash and prevents duplicate indices from ever reaching `CBMT::build_merkle_proof`.

## Proof of Concept

1. Start a CKB node with `LightClient` protocol enabled.
2. Mine a block containing a transaction with hash `h`.
3. Connect as a light-client peer and send:
   ```
   GetTransactionsProof {
     last_hash: <current tip hash>,
     tx_hashes: [h, h]
   }
   ```
4. Observe the node panics at `get_transactions_proof.rs:97` with message `"build proof with verified inputs should be OK"`.

Unit test equivalent: construct a `MockChain`, commit a transaction with hash `h`, send `GetTransactionsProof` with `tx_hashes = [h, h]`, and assert no panic occurs and a `MalformedProtocolMessage` status is returned (currently it panics before returning).

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

**File:** util/types/src/utilities/merkle_tree.rs (L23-23)
```rust
pub type CBMT = ExCBMT<Byte32, MergeByte32>;
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
