Based on my review of the actual code, this is a real vulnerability.

---

### Title
Unbounded Per-Block Work in `GetTransactionsProofProcess::execute` Allows DoS via 1000-Block Fan-Out — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

### Summary
The `GET_TRANSACTIONS_PROOF_LIMIT` guard only bounds the number of requested `tx_hashes`, not the number of distinct blocks those transactions span. An attacker supplying 1000 tx_hashes each confirmed in a different block causes the server to perform 1000 full-block deserializations plus 3000 additional DB reads per single unauthenticated P2P message.

### Finding Description

The limit check at line 37 rejects requests with more than 1000 tx_hashes: [1](#0-0) 

The constant is: [2](#0-1) 

After the check passes, found transactions are grouped by block hash into `txs_in_blocks`: [3](#0-2) 

The loop then iterates over every distinct block, performing four expensive operations per block: [4](#0-3) 

Per iteration:
1. `snapshot.get_block(&block_hash)` — full block deserialization (all transactions)
2. `CBMT::build_merkle_proof(block.transactions().iter()...)` — iterates every tx in the block
3. `block.calc_witnesses_root()` — hashes all witnesses
4. `snapshot.get_block_uncles(&block_hash)` — additional DB read
5. `snapshot.get_block_extension(&block_hash)` — additional DB read

With 1000 tx_hashes each in a distinct block, `txs_in_blocks` has 1000 entries, yielding **≥4000 RocksDB reads and 1000 full block deserializations** per request. All `filtered_block` objects are accumulated in memory before the response is sent.

There is no per-peer rate limiting, no request throttling, and no limit on the number of distinct blocks in `lib.rs`: [5](#0-4) 

The only ban path is for malformed messages; a well-formed oversized request is processed in full.

### Impact Explanation
A single attacker peer can continuously send crafted `GetTransactionsProof` messages. Each message triggers 1000 full-block deserializations from RocksDB, saturating I/O bandwidth and causing large heap allocations. Multiple concurrent connections multiply the effect. This can starve the node of I/O capacity needed for block sync and transaction relay.

### Likelihood Explanation
The attack requires only: (1) light-client protocol enabled, (2) knowledge of 1000 on-chain tx hashes from distinct blocks (trivially obtained from any block explorer). No authentication, no PoW, no privileged access.

### Recommendation
Add a limit on the number of **distinct blocks** in `txs_in_blocks`, e.g.:
```rust
if txs_in_blocks.len() > constant::GET_TRANSACTIONS_PROOF_BLOCK_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many blocks");
}
```
Set `GET_TRANSACTIONS_PROOF_BLOCK_LIMIT` to a small value (e.g., 10–20). Additionally, consider per-peer request rate limiting in the `received` handler.

### Proof of Concept
1. Collect 1000 confirmed tx hashes, each from a different main-chain block.
2. Connect as a light-client peer.
3. Send `GetTransactionsProof { last_hash: <current tip>, tx_hashes: [1000 hashes] }`.
4. Observe: server executes 1000 `get_block` calls + 3000 additional DB reads; RSS spikes proportionally; repeat to sustain I/O saturation.

### Citations

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L37-39)
```rust
        if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
        }
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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L82-126)
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

            positions.push(leaf_index_to_pos(block.number()));
            filtered_blocks.push(filtered_block);

            let uncles = snapshot
                .get_block_uncles(&block_hash)
                .expect("block uncles must be stored");
            let extension = snapshot.get_block_extension(&block_hash);

            uncles_hash.push(uncles.data().calc_uncles_hash());
            extensions.push(packed::BytesOpt::new_builder().set(extension).build());
        }
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
