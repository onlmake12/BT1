The code confirms this is a real vulnerability. Here is the full analysis:

---

### Title
Missing Deduplication in `GetTransactionsProofProcess::execute` Enables CPU/IO Amplification — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

### Summary

`GetTransactionsProofProcess::execute` accepts up to `GET_TRANSACTIONS_PROOF_LIMIT = 1000` tx hashes with no deduplication check. An unprivileged remote peer can send 1000 identical copies of one valid tx hash, causing the server to perform 1000 redundant storage lookups and return a response containing 1000 copies of the same transaction.

### Finding Description

`GetBlocksProofProcess::execute` explicitly builds a `HashSet` and rejects any request containing duplicate hashes: [1](#0-0) 

`GetTransactionsProofProcess::execute` has no equivalent guard. The incoming `tx_hashes` are iterated directly: [2](#0-1) 

Every hash in the `found` partition then triggers a `get_transaction_with_info` call: [3](#0-2) 

The limit is 1000: [4](#0-3) 

With 1000 identical hashes all resolving to the same `block_hash`, the `HashMap` accumulates 1000 `(tx, index)` pairs under one key. `CBMT::build_merkle_proof` is then called with 1000 duplicate indices, and the response is serialized with 1000 copies of the same transaction data: [5](#0-4) 

### Impact Explanation

Per malformed request, the server performs:
- 1000 × `snapshot.get_transaction_info` (disk/RocksDB reads)
- 1000 × `snapshot.get_transaction_with_info` (disk/RocksDB reads)
- `CBMT::build_merkle_proof` with 1000 duplicate indices (CPU)
- A response payload containing 1000 copies of the same transaction (bandwidth)

A single connection can sustain this at the rate the server can process messages. There is no per-peer rate limit visible in the message dispatch path: [6](#0-5) 

### Likelihood Explanation

The light client protocol is a supported production P2P protocol. Any peer that can connect and send a `LightClientMessage::GetTransactionsProof` can trigger this. No authentication, stake, or PoW is required.

### Recommendation

Add a deduplication check mirroring `GetBlocksProofProcess`, immediately after the length check:

```rust
let mut uniq = std::collections::HashSet::new();
if !self.message.tx_hashes().to_entity().into_iter().all(|h| uniq.insert(h)) {
    return StatusCode::MalformedProtocolMessage.with_context("duplicate tx hash exists");
}
```

This is exactly the pattern already used in `GetBlocksProofProcess`: [1](#0-0) 

### Proof of Concept

1. Connect to a CKB node with the light client protocol enabled.
2. Obtain any confirmed tx hash `H`.
3. Send a `GetTransactionsProof` message with `tx_hashes = [H; 1000]` and a valid `last_hash`.
4. Instrument `snapshot.get_transaction_info`: it will be called exactly 1000 times, not 1.
5. The server returns a `SendTransactionsProof` response containing 1000 copies of the same transaction.

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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L82-102)
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

            let txs: Vec<_> = txs_and_tx_indices
                .into_iter()
                .map(|(tx, _)| tx.data())
                .collect();
```

**File:** util/light-client-protocol-server/src/constant.rs (L7-7)
```rust
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/lib.rs (L55-92)
```rust
    async fn received(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        data: Bytes,
    ) {
        trace!("LightClient.received peer={}", peer);

        let msg = match packed::LightClientMessageReader::from_slice(&data) {
            Ok(msg) => msg.to_enum(),
            _ => {
                warn!(
                    "LightClient.received a malformed message from Peer({})",
                    peer
                );
                nc.ban_peer(
                    peer,
                    constant::BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };

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
