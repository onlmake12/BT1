Audit Report

## Title
Unbounded Per-Block Work Amplification in `GetTransactionsProofProcess::execute` Enables Sustained DoS — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

## Summary
The `execute` function in `GetTransactionsProofProcess` enforces a limit of 1000 `tx_hashes` per request but performs O(N × block_tx_count) CPU work and 3×N database reads where N is the number of distinct blocks spanned. An unprivileged peer can craft a single `GetTransactionsProof` message that forces the server to read 1000 full blocks, run `CBMT::build_merkle_proof` and `calc_witnesses_root` over every transaction in each block, and generate a 1000-position MMR proof — all before sending any response. No per-peer rate limiting exists in the light client protocol dispatcher.

## Finding Description
The only input validation is the tx_hash count check at lines 37–39:

```rust
if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
}
```

`GET_TRANSACTIONS_PROOF_LIMIT` is 1000 (`constant.rs` line 7). If all 1000 tx_hashes belong to 1000 distinct main-chain blocks, `txs_in_blocks` has 1000 entries. The loop at lines 82–126 then performs per block:

1. **`snapshot.get_block(&block_hash)`** (lines 83–85) — full block deserialization from DB, loading all transactions.
2. **`CBMT::build_merkle_proof(...)`** (lines 86–97) — iterates and hashes **all** transactions in the block to build the proof, not just the requested one.
3. **`block.calc_witnesses_root()`** (line 106) — hashes **all** witnesses in the block.
4. **`snapshot.get_block_uncles`** and **`snapshot.get_block_extension`** (lines 119–122) — two additional DB reads per block.

After the loop, `reply_proof` (lib.rs lines 207–217) calls `mmr.gen_proof(items_positions)` with up to 1000 positions, performing O(1000 × log(chain_length)) MMR node reads.

By contrast, the analogous `GetBlocksProofProcess::execute` (get_blocks_proof.rs line 83) only reads block **headers** via `get_block_header`, not full blocks. The work ratio between the two handlers for the same item count is O(block_tx_count), which can be hundreds of transactions per block.

The `try_process` dispatcher in `lib.rs` (lines 96–125) applies no per-peer rate limiting, request queuing, or work budget to `GetTransactionsProof` messages. The `Relayer` protocol has a `rate_limiter` field but this is a separate protocol entirely and does not apply here.

**Exploit flow:**
1. Attacker connects as a normal P2P peer to a CKB full node running the light client server.
2. Attacker collects 1000 confirmed tx_hashes, one from each of 1000 different large main-chain blocks (all public data).
3. Attacker sends a single `GetTransactionsProof` message with all 1000 hashes and a valid `last_hash`.
4. Server performs: 1000 full block DB reads + O(1000 × max_block_tx_count) hash operations + 1000 uncle/extension reads + O(1000 × log N) MMR reads.
5. Attacker repeats at maximum connection rate, potentially across multiple connections.

## Impact Explanation
This is a **High** severity finding matching the allowed impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."* / *"Vulnerabilities which could easily crash a CKB node."*

The light client server runs within the CKB full node process. Saturating its I/O and CPU with repeated maximally-crafted requests can starve the node's other subsystems (sync, relay, tx-pool) of I/O bandwidth and CPU time, causing the node to become unresponsive or crash under sustained load. The cost to the attacker is negligible: only publicly available on-chain data and a P2P connection are required.

## Likelihood Explanation
- Any unprivileged peer can trigger this; no authentication or special role is required.
- All required tx_hashes are publicly visible on-chain and trivially scraped.
- The P2P message path is open to any connecting peer.
- No per-peer rate limiting, request throttling, or work budget exists in the handler or dispatcher.
- The attack is trivially repeatable and parallelizable across multiple connections.
- The amplification factor over `GetBlocksProof` (which reads only headers) is proportional to block transaction count, making this a targeted amplification vector.

## Recommendation
1. **Cap the number of distinct blocks** spanned by a single request independently of the tx_hash count (e.g., ≤ 50 distinct blocks per request).
2. **Add per-peer rate limiting** at the `try_process` dispatcher level for `GetTransactionsProof` messages, analogous to the `rate_limiter` used in the `Relayer` protocol.
3. Alternatively, enforce a **byte-budget** per request proportional to total block sizes rather than a flat tx_hash count.

## Proof of Concept
```
1. Connect to a CKB full node with light client protocol enabled.
2. Collect 1000 confirmed tx_hashes, one from each of 1000 different
   main-chain blocks with large transaction counts (public data from
   any block explorer or full node RPC).
3. Construct a GetTransactionsProof P2P message:
     tx_hashes = [h_1, h_2, ..., h_1000]  (one per distinct block)
     last_hash  = current tip hash
4. Send the message and measure server-side CPU and I/O:
   - Expected: 1000 get_block() calls, 1000 CBMT proof builds over
     all block txs, 1000 calc_witnesses_root() calls, 1000 uncle +
     extension reads, O(1000 × log N) MMR reads.
5. Repeat at maximum rate across multiple connections.
6. Compare CPU/IO load against an equivalent GetBlocksProof request
   with 1000 hashes (which only reads headers) to quantify the
   amplification factor.
```