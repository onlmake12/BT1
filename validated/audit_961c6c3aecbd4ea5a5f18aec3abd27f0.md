Audit Report

## Title
Missing Deduplication Guard in `GetTransactionsProofProcess::execute` Enables Unbounded Work Amplification — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

## Summary

`GetTransactionsProofProcess::execute` applies no deduplication to the incoming `tx_hashes` list, unlike the analogous `GetBlocksProofProcess::execute` which explicitly rejects duplicate hashes. An unprivileged remote peer with an open LightClient session can send a `GetTransactionsProof` message containing up to 1000 identical confirmed transaction hashes, forcing the server to perform 2000 database reads, CBMT proof construction over 1000 identical indices, and response serialization of 1000 copies of the same transaction — with no ban applied and no rate limiter on the LightClient handler.

## Finding Description

`GetBlocksProofProcess::execute` explicitly deduplicates its input at lines 62–70 of `get_blocks_proof.rs`:

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

`GetTransactionsProofProcess::execute` has no equivalent guard. The only input validation is an empty check and a count ceiling of 1000 (lines 33–39 of `get_transactions_proof.rs`):

```rust
if self.message.tx_hashes().is_empty() { ... }
if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT { ... }
```

With 1000 identical confirmed tx hashes, the full execution path proceeds:

1. **Partition (L54–64):** Iterates all 1000 hashes, calling `snapshot.get_transaction_info(tx_hash)` and `snapshot.is_main_chain(...)` for each — 1000 DB reads.
2. **HashMap build (L66–75):** Calls `snapshot.get_transaction_with_info(&tx_hash)` for each of the 1000 hashes — 1000 more DB reads. All 1000 `(tx, index)` pairs land in the same block's `Vec`.
3. **CBMT proof (L86–97):** `CBMT::build_merkle_proof` is called with a `Vec` of 1000 identical `u32` indices.
4. **Response serialization (L99–114):** A `FilteredBlock` is built containing 1000 copies of the same transaction data, then serialized and sent.

The `LightClientProtocol` handler in `lib.rs` (L81–86) only bans a peer when `status.should_ban()` returns `Some(...)`, which requires a 4xx status code. Since no `MalformedProtocolMessage` is ever returned for duplicate tx hashes, no ban is triggered. Additionally, unlike the `Relayer` protocol which has a per-peer rate limiter, the `LightClientProtocol` handler has no rate limiting at all.

## Impact Explanation

This is a **High** severity issue matching: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

A single attacker connection can sustain 2000 DB reads and a large serialized response per request, repeated indefinitely without ban. The response amplification (1000 copies of the same transaction) also amplifies outbound bandwidth from the server. Under sustained attack from even a single peer, this can exhaust the node's I/O capacity and degrade or crash the LightClient-serving node.

## Likelihood Explanation

The attack requires only:
- An open LightClient P2P session (publicly available)
- Knowledge of one confirmed on-chain transaction hash (trivially obtainable from any block explorer or by querying the node itself)

No privileged access, hashpower, or key material is needed. The message is well-formed and passes all existing validation. The attacker is never banned, so the attack can be sustained indefinitely from a single connection.

## Recommendation

Add a deduplication check immediately after the count check in `GetTransactionsProofProcess::execute`, mirroring `GetBlocksProofProcess`:

```rust
let tx_hashes_vec: Vec<_> = self.message.tx_hashes().to_entity().into_iter().collect();
let mut uniq = HashSet::new();
if !tx_hashes_vec.iter().all(|h| uniq.insert(h)) {
    return StatusCode::MalformedProtocolMessage
        .with_context("duplicate tx hash exists");
}
```

This returns a 4xx status, triggering `should_ban()` and a peer ban via `nc.ban_peer(...)`, and eliminates the amplification. Additionally, consider adding a per-peer rate limiter to the `LightClientProtocol` handler consistent with the approach used in the `Relayer` protocol.

## Proof of Concept

1. Connect to a CKB node with the LightClient protocol enabled.
2. Obtain any confirmed on-chain transaction hash `h` (e.g., from a block explorer or `get_transaction` RPC).
3. Obtain the current tip hash `tip_hash`.
4. Send a `GetTransactionsProof { last_hash: tip_hash, tx_hashes: [h; 1000] }` message over the LightClient P2P session.
5. Observe the server performs 2000 DB reads (1000 × `get_transaction_info` + 1000 × `get_transaction_with_info`) and returns a `SendTransactionsProof` response containing 1000 copies of the same transaction.
6. Confirm no ban is applied to the sending peer.
7. Repeat in a tight loop and measure server CPU, I/O, and outbound bandwidth vs. a single-hash request to confirm the O(N) amplification ratio.