The code is concrete and fully readable. Here is the analysis:

---

**Tracing the path:**

1. Any peer sends `GetTransactionsProof` with up to 1000 `tx_hashes`. The guard at line 37 rejects `> 1000`, so exactly 1000 is accepted. [1](#0-0) [2](#0-1) 

2. The `found` partition calls `get_transaction_info` for each hash (1000 lightweight DB reads). [3](#0-2) 

3. `txs_in_blocks` is a `HashMap` keyed by `block_hash`. If all 1000 tx_hashes are in 1000 distinct blocks, the map has 1000 entries. [4](#0-3) 

4. The loop at line 82 then, **for each of the 1000 blocks**, calls:
   - `snapshot.get_block(&block_hash)` — full block deserialization (all transactions) [5](#0-4) 
   - `CBMT::build_merkle_proof(block.transactions().iter()...)` — iterates **every** transaction in the block to build the proof [6](#0-5) 
   - `block.calc_witnesses_root()` — iterates all transactions again [7](#0-6) 
   - `snapshot.get_block_uncles(&block_hash)` — additional DB read [8](#0-7) 
   - `snapshot.get_block_extension(&block_hash)` — additional DB read [9](#0-8) 

5. There is **no rate limiting, no per-peer throttling, and no per-request timeout** visible anywhere in `received` or `try_process`. [10](#0-9) 

---

**Why the limit does not bound the work:**

`GET_TRANSACTIONS_PROOF_LIMIT = 1000` bounds the number of *transactions*, not the number of *distinct blocks*. The work per request is `O(N_blocks × avg_block_size)`, not `O(N_txs)`. With 1000 transactions each in a different block, the server performs ≥3000 RocksDB reads and deserializes 1000 full block objects simultaneously. An attacker can repeat this in a tight loop from a single connection.

---

### Title
Unbounded Full-Block Deserialization per Distinct Block in `GetTransactionsProofProcess::execute` — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

### Summary
An unprivileged remote peer can send a single `GetTransactionsProof` message with 1000 `tx_hashes` each confirmed in a different main-chain block, causing the server to deserialize 1000 full blocks, run `CBMT::build_merkle_proof` and `calc_witnesses_root` over every transaction in each block, and issue 3000+ additional RocksDB reads — all within a single async handler with no rate limiting.

### Finding Description
`GET_TRANSACTIONS_PROOF_LIMIT` (= 1000) is checked against the number of requested transaction hashes, not the number of distinct blocks those transactions span. The loop at line 82 iterates `txs_in_blocks`, which can have up to 1000 entries (one per distinct block). For each entry it calls `get_block` (full deserialization), `CBMT::build_merkle_proof` (O(block_tx_count)), `calc_witnesses_root` (O(block_tx_count)), `get_block_uncles`, and `get_block_extension`. All 1000 full block objects are live in memory simultaneously before the response is sent.

### Impact Explanation
- **Memory**: 1000 full `BlockView` objects in memory at once; blocks on mainnet can be large.
- **I/O**: ≥3000 synchronous RocksDB reads per request; repeated requests from one or more peers cause I/O starvation for all other node operations (sync, tx-pool, RPC).
- **CPU**: `CBMT::build_merkle_proof` and `calc_witnesses_root` iterate all transactions in each block; for blocks with hundreds of transactions this is non-trivial.
- No authentication or rate limit prevents an attacker from issuing this request continuously.

### Likelihood Explanation
- Light-client protocol is a production feature enabled by operator configuration.
- The attacker only needs 1000 confirmed tx_hashes in distinct blocks, trivially obtained from any public block explorer.
- No PoW, no stake, no privileged role required — any TCP peer suffices.

### Recommendation
1. Add a **distinct-block limit** (e.g., ≤ 32 or ≤ 64 blocks per request) checked before the expensive loop.
2. Alternatively, restructure the response to use only per-transaction data already fetched (avoid `get_block` entirely by storing the Merkle proof at index time).
3. Add per-peer request rate limiting in the `received` handler for light-client messages.

### Proof of Concept
```
1. Collect 1000 tx_hashes from 1000 distinct main-chain blocks (block explorer).
2. Connect to a node with light-client protocol enabled.
3. Send: GetTransactionsProof { last_hash: <current tip>, tx_hashes: [h1..h1000] }
4. Observe: server calls get_block() 1000 times, get_block_uncles() 1000 times,
   get_block_extension() 1000 times, CBMT::build_merkle_proof() 1000 times.
5. Repeat in a loop; measure RocksDB read latency and node RSS growth.
```

### Citations

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L37-39)
```rust
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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L119-121)
```rust
            let uncles = snapshot
                .get_block_uncles(&block_hash)
                .expect("block uncles must be stored");
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L122-122)
```rust
            let extension = snapshot.get_block_extension(&block_hash);
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
