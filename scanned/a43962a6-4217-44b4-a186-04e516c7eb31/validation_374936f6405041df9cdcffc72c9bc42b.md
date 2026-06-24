The code has been verified against all cited claims. Every line reference is accurate:

- Lines 37-39: limit check on tx_hashes count only [1](#0-0) 
- Lines 66-75: grouping by block_hash with no cap on distinct blocks [2](#0-1) 
- Lines 86-97: `CBMT::build_merkle_proof` fed all block transactions per block [3](#0-2) 
- Lines 106, 119-122: `calc_witnesses_root()` and `get_block_uncles()` called per block [4](#0-3) 
- `lib.rs` lines 118-122: no rate limiting or per-peer quota at dispatch [5](#0-4) 
- `constant.rs` line 7: `GET_TRANSACTIONS_PROOF_LIMIT = 1000` [6](#0-5) 

---

Audit Report

## Title
CPU Amplification DoS via Unbounded CBMT Work in `GetTransactionsProofProcess::execute` — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

## Summary

The `GetTransactionsProof` handler enforces a limit of 1000 on the number of requested tx_hashes, but performs no cap on the number of distinct blocks those hashes may span. For each distinct block, `CBMT::build_merkle_proof` is called with **all** transactions in that block as leaves, plus `calc_witnesses_root()` and `get_block_uncles()` per block. An attacker sending 1000 tx_hashes each from a different high-transaction block multiplies the server's CPU work by `avg_txs_per_block`, causing sustained CPU exhaustion with no rate limiting in place.

## Finding Description

The limit check at lines 37–39 only bounds the count of incoming tx_hashes:

```rust
if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
}
```

After this check, found hashes are grouped into a `HashMap` keyed by `block_hash` (lines 66–75), with no bound on the number of distinct blocks. The loop at line 82 then iterates over every distinct block and calls:

```rust
CBMT::build_merkle_proof(
    &block.transactions().iter().map(|tx| tx.hash()).collect::<Vec<_>>(),
    ...
)
```

The leaf input is **every transaction in the block**, not just the requested ones. Additionally, `block.calc_witnesses_root()` (line 106) and `snapshot.get_block_uncles()` (line 119) are called once per block. There is no cap on distinct blocks, no per-peer rate limiting, and no total-work budget anywhere in the handler or the dispatcher (`lib.rs` lines 118–122).

## Impact Explanation

Maps to **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, and potentially **High: Vulnerabilities which could easily crash a CKB node**.

Total CBMT work per request is `O(GET_TRANSACTIONS_PROOF_LIMIT × avg_txs_per_block)`. On mainnet blocks with 500 transactions, a single crafted request triggers ~500,000 hash operations. Pipelining multiple such requests from one or more peers causes sustained CPU exhaustion, degrading or crashing the node's ability to serve any protocol traffic.

## Likelihood Explanation

- Any peer speaking the light client protocol can send `GetTransactionsProof`; no privilege is required.
- Required tx_hashes are public chain data, trivially collected by observing the chain.
- The message is structurally valid, so the ban-on-malformed-message path is never triggered.
- No rate limiting or per-peer quota exists at any layer.
- The attack is fully repeatable and can be sustained indefinitely.

## Recommendation

1. **Cap the number of distinct blocks** a single request may span (e.g., to 10–20), rejecting requests that exceed this after the grouping step.
2. Alternatively, accumulate `block.transactions().len()` across all referenced blocks and reject if the total exceeds a threshold (e.g., 10,000 total transactions).
3. Add per-peer rate limiting on `GetTransactionsProof` messages at the protocol dispatcher level in `lib.rs`.

## Proof of Concept

1. Identify 1000 mainnet blocks each containing 500+ transactions.
2. From each block, select one transaction hash.
3. Construct a `GetTransactionsProof` message with all 1000 tx_hashes and a valid `last_hash`.
4. Send the message to a CKB node serving the light client protocol.
5. Observe: `CBMT::build_merkle_proof` is called 1000 times, each iterating over 500+ transaction hashes — approximately 500,000 hash operations for a single request, versus the ~1000 the limit was intended to bound.
6. Repeat at maximum message rate; CPU usage scales linearly with block fullness and request rate, with no throttle to stop it.

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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L106-122)
```rust
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
```

**File:** util/light-client-protocol-server/src/lib.rs (L118-122)
```rust
            packed::LightClientMessageUnionReader::GetTransactionsProof(reader) => {
                components::GetTransactionsProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
```

**File:** util/light-client-protocol-server/src/constant.rs (L7-7)
```rust
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```
