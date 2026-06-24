Audit Report

## Title
Unbounded Per-Request DB/CPU Work in `GetTransactionsProofProcess::execute` with No Rate Limiting — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

## Summary
Any peer speaking the light-client protocol can send a single `GetTransactionsProof` message with up to 1000 transaction hashes, each from a distinct block. The server performs up to 1000 full block deserializations, 1000 CBMT merkle proof builds, 1000 witness-root computations, 2000 additional DB reads, and one O(1000 × log N) MMR proof generation — all synchronously, with no per-peer rate limiting. Repeated or concurrent requests from one or more peers can saturate the node's I/O and CPU, degrading or halting block processing.

## Finding Description

**Entrypoint:** `LightClientProtocol::received` (lib.rs L55–92) parses the message and calls `try_process` (lib.rs L96–125) with no rate check. `try_process` dispatches directly to `GetTransactionsProofProcess::execute` for any `GetTransactionsProof` message. [1](#0-0) 

**Guards inside `execute` are insufficient:** The only checks are (1) reject empty `tx_hashes`, (2) reject if `len > GET_TRANSACTIONS_PROOF_LIMIT` (1000), and (3) reject if `last_hash` is not on the main chain. None of these limit request rate or per-peer work. [2](#0-1) [3](#0-2) 

**Per-block work loop (lines 82–126):** For each distinct block containing a requested transaction, the server calls `snapshot.get_block`, `CBMT::build_merkle_proof`, `block.calc_witnesses_root`, `snapshot.get_block_uncles`, and `snapshot.get_block_extension`. With 1000 tx hashes each in a different block, this loop executes 1000 times. [4](#0-3) 

**MMR proof generation:** `reply_proof` calls `mmr.gen_proof(items_positions)` where `items_positions` can hold up to 1000 leaf positions, resulting in O(1000 × log N) MMR node reads from the DB. [5](#0-4) 

**No rate limiting in `LightClientProtocol`:** A search for `rate_limiter`, `TooManyRequests`, `check_key`, and `RateLimiter` across the entire `util/light-client-protocol-server/` tree returns zero matches. By contrast, `Relayer::try_process` applies a per-peer, per-message-type rate limiter before any processing: [6](#0-5) 

`LightClientProtocol` has no equivalent guard. [1](#0-0) 

## Impact Explanation

A single well-crafted request forces 1000 full block deserializations (each block potentially hundreds of KB), 1000 CBMT proof builds, 1000 witness root hash computations, 2000 additional DB reads, and one large MMR proof generation. An attacker sending this in a tight loop, or from multiple peers simultaneously, can saturate the node's RocksDB I/O and CPU, causing the node to stop processing new blocks and peer connections — matching the allowed impact: **"Vulnerabilities which could easily crash a CKB node" (High, 10001–15000 points)**.

## Likelihood Explanation

- No proof-of-work, stake, or privileged role is required — any peer that speaks the light-client protocol can send this message.
- 1000 confirmed transaction hashes from 1000 different blocks are trivially obtainable from any public block explorer or via the node's own RPC.
- The attack is repeatable at will; no server-side cooldown or ban is triggered by a well-formed (but maximally expensive) request.
- The light-client protocol is a production feature enabled on full nodes that opt in via `support_protocols`.

## Recommendation

1. **Add a per-peer rate limiter** to `LightClientProtocol::try_process` mirroring the one in `Relayer::try_process` (e.g., `governor::RateLimiter` keyed by `(PeerIndex, message_item_id)`).
2. **Cap the number of distinct blocks** a single `GetTransactionsProof` request may span (e.g., 10–50), independent of the total `tx_hash` count.
3. **Bound MMR proof generation** by rejecting requests whose `positions` vector exceeds a separate, smaller limit before calling `mmr.gen_proof`.

## Proof of Concept

```
1. Run a CKB full node with light-client protocol enabled.
2. Collect 1000 confirmed transaction hashes, one from each of 1000 different blocks
   (available from any block explorer or via get_block RPC).
3. Connect as a light-client peer and send:
     GetTransactionsProof {
         last_hash: <current tip hash>,
         tx_hashes: [tx_hash_block_1, tx_hash_block_2, ..., tx_hash_block_1000]
     }
4. Observe: the server performs 1000 get_block DB reads, 1000 CBMT proof builds,
   1000 calc_witnesses_root calls, 2000 uncle/extension reads, and one
   mmr.gen_proof(1000 positions) call — all in a single synchronous request handler.
5. Repeat in a tight loop (or from multiple peers) to sustain CPU/IO saturation.
   No server-side throttle fires.
```

### Citations

**File:** util/light-client-protocol-server/src/lib.rs (L96-125)
```rust
    async fn try_process(
        &mut self,
        nc: &Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
        message: packed::LightClientMessageUnionReader<'_>,
    ) -> Status {
        match message {
            packed::LightClientMessageUnionReader::GetLastState(reader) => {
                components::GetLastStateProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            packed::LightClientMessageUnionReader::GetLastStateProof(reader) => {
                components::GetLastStateProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            packed::LightClientMessageUnionReader::GetBlocksProof(reader) => {
                components::GetBlocksProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            packed::LightClientMessageUnionReader::GetTransactionsProof(reader) => {
                components::GetTransactionsProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            _ => StatusCode::UnexpectedProtocolMessage.into(),
        }
    }
```

**File:** util/light-client-protocol-server/src/lib.rs (L207-217)
```rust
            let proof = if items_positions.is_empty() {
                Default::default()
            } else {
                match mmr.gen_proof(items_positions) {
                    Ok(proof) => proof.proof_items().to_owned(),
                    Err(err) => {
                        let errmsg = format!("failed to generate a proof since {err:?}");
                        return StatusCode::InternalError.with_context(errmsg);
                    }
                }
            };
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L33-49)
```rust
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

**File:** sync/src/relayer/mod.rs (L113-123)
```rust
        let should_check_rate =
            !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));

        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```
