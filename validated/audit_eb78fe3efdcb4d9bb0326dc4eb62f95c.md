### Title
Unbounded Per-Request Work Amplification in `GetTransactionsProofProcess::execute` Enables Sustained DoS — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

### Summary
The `execute` function enforces a count limit of 1000 tx_hashes but performs O(N × block_tx_count) CPU work and 3× N DB reads, where N is the number of distinct blocks spanned. An unprivileged peer can craft a single `GetTransactionsProof` message that causes the server to read 1000 full blocks from the database, run `CBMT::build_merkle_proof` and `calc_witnesses_root` over every transaction in each of those blocks, and then generate a 1000-position MMR proof — all before sending any response.

### Finding Description

The limit check at lines 37–39 only bounds the number of requested tx_hashes: [1](#0-0) 

`GET_TRANSACTIONS_PROOF_LIMIT` is set to 1000: [2](#0-1) 

If all 1000 tx_hashes belong to 1000 distinct main-chain blocks, `txs_in_blocks` has 1000 entries. The subsequent loop then performs, **per block**:

1. `snapshot.get_block(&block_hash)` — full block deserialization from DB (all transactions): [3](#0-2) 

2. `CBMT::build_merkle_proof(...)` — iterates **all** transactions in the block, not just the requested one: [4](#0-3) 

3. `block.calc_witnesses_root()` — hashes **all** witnesses in the block: [5](#0-4) 

4. `get_block_uncles` and `get_block_extension` — two more DB reads: [6](#0-5) 

After the loop, `reply_proof` is called with 1000 positions, generating an MMR proof of O(1000 × log(chain_length)) reads.

### Impact Explanation

Total work per single request with 1000 tx_hashes from 1000 distinct large blocks:
- **3000+ DB reads** (get_block, get_block_uncles, get_block_extension × 1000)
- **O(1000 × max_block_tx_count) hash operations** for CBMT and witnesses root
- **O(1000 × log N) MMR node reads** for the proof

The limit check implies O(1000) work; the actual work is O(1000 × block_size). A single attacker repeatedly sending such requests can saturate the light-client server's I/O and CPU, causing it to become unresponsive to legitimate light clients.

### Likelihood Explanation

- All required tx_hashes are publicly visible on-chain; no privileged access is needed.
- The P2P message path is open to any peer.
- No per-peer rate limiting or request throttling is present in the handler.
- The attack is trivially repeatable and can be parallelized across multiple connections.

### Recommendation

1. **Cap the number of distinct blocks** spanned by a single request (e.g., ≤ 50 blocks), independent of the tx_hash count limit.
2. Alternatively, **charge work proportional to total block sizes** and enforce a byte-budget per request.
3. Add **per-peer rate limiting** at the protocol dispatcher level for `GetTransactionsProof` messages.

### Proof of Concept

```
1. Collect 1000 confirmed tx_hashes, one from each of 1000 different main-chain blocks
   (all public data, trivially scraped from any block explorer or full node).
2. Send a single GetTransactionsProof P2P message with:
     tx_hashes = [h_1, h_2, ..., h_1000]   (one per distinct block)
     last_hash  = current tip hash
3. Observe server-side: 3000+ DB reads, O(1000 × block_tx_count) SHA3 calls,
   O(1000 × log N) MMR reads — all before any byte is sent back.
4. Repeat at the maximum rate the connection allows.
5. Compare CPU and I/O load against a GetBlocksProof request with 1000 hashes
   to quantify the amplification factor.
```

### Citations

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
