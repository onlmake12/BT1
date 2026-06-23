### Title
Unbounded Work Amplification in `GetTransactionsProofProcess::execute` with No Rate Limiting — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

---

### Summary

`GetTransactionsProofProcess::execute` performs O(N × block_tx_count) CPU work and O(N) full-block DB reads per request, where N ≤ 1000. The `LightClientProtocol` handler has **no rate limiter**, unlike every other comparable protocol handler in the codebase. An unprivileged peer can repeatedly send crafted `GetTransactionsProof` messages to cause sustained CPU exhaustion and I/O saturation on the server.

---

### Finding Description

**Entrypoint:** Any peer connected on the light-client P2P protocol can send a `GetTransactionsProof` message. The handler is dispatched unconditionally in `LightClientProtocol::try_process`: [1](#0-0) 

The only guard is a count check: [2](#0-1) 

`GET_TRANSACTIONS_PROOF_LIMIT` is 1000: [3](#0-2) 

**Expensive inner loop — full block reads + CBMT + witnesses root:**

For each distinct block that contains a requested transaction, the handler:

1. Reads the **full block** (all transactions) from the DB: [4](#0-3) 

2. Calls `CBMT::build_merkle_proof`, which iterates over **every transaction hash in the block** to build the tree: [5](#0-4) 

3. Calls `block.calc_witnesses_root()`, which hashes **every witness in the block**: [6](#0-5) 

4. Reads uncles and extension for each block: [7](#0-6) 

With 1000 tx_hashes each from a different large block, this is 1000 full-block DB reads + O(Σ block_tx_count) hashing work + 2000 additional DB reads (uncles + extensions) + O(1000 × log N) MMR reads in `reply_proof`: [8](#0-7) 

**Contrast with `GetBlocksProof`:** The analogous handler reads only block *headers* (lightweight), not full blocks: [9](#0-8) 

It performs no CBMT or witnesses-root computation at all.

**No rate limiting on `LightClientProtocol`:**

The `received` handler dispatches directly to `try_process` with zero rate-limiting: [10](#0-9) 

By contrast, `Relayer` and `HolePunching` both maintain per-peer, per-message-type `RateLimiter` instances (30 req/sec hard cap). The grep for `rate_limit|RateLimiter|quota|per_second` returns **zero matches** in the entire `util/light-client-protocol-server/` tree.

---

### Impact Explanation

A single attacker peer can:
1. Collect 1000 valid on-chain tx hashes, one from each of 1000 different large blocks (all public data).
2. Repeatedly send `GetTransactionsProof` messages at line rate.
3. Each request forces the server to read up to 1000 full blocks from RocksDB, run CBMT and witnesses-root hashing over all their transactions, and generate a 1000-leaf MMR proof.

This causes sustained CPU saturation (hashing) and I/O saturation (full-block reads), degrading or halting service for legitimate light clients. The work per request is O(1000 × max_block_tx_count), not O(1000) as the limit implies.

---

### Likelihood Explanation

- Requires only a standard P2P connection — no PoW, no keys, no privileged role.
- All required tx hashes are publicly visible on-chain.
- No rate limiting exists to throttle repeated requests from the same peer.
- The light client protocol server is a production feature enabled on full nodes that serve light clients.

---

### Recommendation

1. **Add per-peer rate limiting** to `LightClientProtocol::received`, mirroring the `Relayer` and `HolePunching` patterns (governor `RateLimiter` keyed by `(PeerIndex, message_item_id)`).
2. **Cap the number of distinct blocks** a single `GetTransactionsProof` request may span (e.g., ≤ 10–20 blocks), independent of the tx count limit.
3. Consider replacing `get_block` with a targeted transaction fetch that avoids loading the entire block body when only a few transactions per block are needed.

---

### Proof of Concept

```
# Precondition: node has ≥1000 blocks, each with ≥1 transaction.
# Collect one tx_hash from each of 1000 different blocks via RPC.
tx_hashes = [get_block_by_number(n).transactions[0].hash for n in range(1, 1001)]

# Craft GetTransactionsProof with last_hash = current tip
msg = GetTransactionsProof {
    last_hash: tip_hash,
    tx_hashes: tx_hashes,   # 1000 entries, all from distinct blocks
}

# Send in a tight loop from a single peer connection
while True:
    send_light_client_message(msg)
    # No server-side throttle; each request triggers:
    # - 1000 get_block() DB reads
    # - 1000 CBMT::build_merkle_proof() calls
    # - 1000 calc_witnesses_root() calls
    # - 2000 get_block_uncles/get_block_extension DB reads
    # - gen_proof(1000 positions) MMR traversal
```

Monitor server CPU and RocksDB read latency; compare against an equivalent `GetBlocksProof` with 1000 block hashes to quantify the amplification ratio.

### Citations

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

**File:** util/light-client-protocol-server/src/lib.rs (L118-122)
```rust
            packed::LightClientMessageUnionReader::GetTransactionsProof(reader) => {
                components::GetTransactionsProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
```

**File:** util/light-client-protocol-server/src/lib.rs (L207-216)
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
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L37-39)
```rust
        if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
        }
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L83-85)
```rust
            let block = snapshot
                .get_block(&block_hash)
                .expect("block should be in store");
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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L106-106)
```rust
                .witnesses_root(block.calc_witnesses_root())
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L119-125)
```rust
            let uncles = snapshot
                .get_block_uncles(&block_hash)
                .expect("block uncles must be stored");
            let extension = snapshot.get_block_extension(&block_hash);

            uncles_hash.push(uncles.data().calc_uncles_hash());
            extensions.push(packed::BytesOpt::new_builder().set(extension).build());
```

**File:** util/light-client-protocol-server/src/constant.rs (L7-7)
```rust
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L82-86)
```rust
            let header = snapshot
                .get_block_header(&block_hash)
                .expect("header should be in store");
            positions.push(leaf_index_to_pos(header.number()));
            block_headers.push(header.data());
```
