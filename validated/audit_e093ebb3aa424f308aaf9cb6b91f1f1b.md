Audit Report

## Title
Missing Deduplication in `GetTransactionsProofProcess::execute` Enables 1000x CPU/IO Amplification — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

## Summary

`GetTransactionsProofProcess::execute` accepts up to 1000 tx hashes with no deduplication check, while the structurally identical `GetBlocksProofProcess::execute` explicitly rejects duplicates via a `HashSet` guard. An unprivileged remote peer can send 1000 identical copies of one valid tx hash, forcing the server to perform 2000 redundant RocksDB reads, invoke `CBMT::build_merkle_proof` with 1000 duplicate indices, and serialize a response containing 1000 copies of the same transaction — all at negligible attacker cost.

## Finding Description

`GetBlocksProofProcess::execute` builds a `HashSet` and returns `MalformedProtocolMessage` on any duplicate: [1](#0-0) 

`GetTransactionsProofProcess::execute` has no equivalent guard. After the length check, `tx_hashes` are iterated directly without deduplication: [2](#0-1) 

The `partition` call at lines 54–64 invokes `snapshot.get_transaction_info` for every hash in the input — 1000 RocksDB reads for 1000 identical hashes: [3](#0-2) 

Every hash in the `found` partition triggers a `get_transaction_with_info` call, and results are pushed into a `Vec` under the block's `HashMap` key — duplicates are not collapsed, yielding 1000 more RocksDB reads and a 1000-element Vec of identical `(tx, index)` pairs: [4](#0-3) 

`CBMT::build_merkle_proof` is then called with a 1000-element vector of the same index, and the response is serialized with 1000 copies of the same transaction: [5](#0-4) 

The message is well-formed, so it passes the parse check in `received` and returns `Status::ok()` — no ban is triggered: [6](#0-5) 

## Impact Explanation

**High — bad design which could cause CKB network congestion with few costs.**

Per single malformed request the server performs: 1000 × `snapshot.get_transaction_info` (RocksDB reads), 1000 × `snapshot.get_transaction_with_info` (RocksDB reads), `CBMT::build_merkle_proof` with 1000 duplicate indices (CPU), and returns a response payload containing 1000 copies of the same transaction (bandwidth amplification). The attacker's cost is one TCP message; the server's cost is 1000× that of a legitimate single-hash request. This matches the allowed bounty impact: **High (10001–15000 points) — vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation

The light client protocol is a supported production P2P protocol reachable by any peer. No authentication, stake, or proof-of-work is required. The limit of 1000 is confirmed in `constant.rs`: [7](#0-6) 

A well-formed `GetTransactionsProof` message with 1000 duplicate hashes passes all current checks cleanly and returns `Status::ok()`, so no ban is applied. An attacker can sustain this in a tight loop from a single connection indefinitely.

## Recommendation

Add a deduplication check immediately after the length check at line 39, mirroring the pattern in `GetBlocksProofProcess`:

```rust
let tx_hashes: Vec<_> = self.message.tx_hashes().to_entity().into_iter().collect();
let mut uniq = std::collections::HashSet::new();
if !tx_hashes.iter().all(|h| uniq.insert(h)) {
    return StatusCode::MalformedProtocolMessage
        .with_context("duplicate tx hash exists");
}
```

Note: `MalformedProtocolMessage` (status code 400) falls in the 4xx range and will trigger a peer ban via `should_ban()`: [8](#0-7) 

This means applying the fix also causes offending peers to be banned for `BAD_MESSAGE_BAN_TIME`, providing a deterrent against repeated abuse.

## Proof of Concept

1. Connect to a CKB node with the light client protocol enabled.
2. Obtain any confirmed tx hash `H` on the main chain.
3. Send a `GetTransactionsProof` message with `tx_hashes = [H; 1000]` and a valid `last_hash` pointing to a main-chain block.
4. Instrument `snapshot.get_transaction_info` and `snapshot.get_transaction_with_info`: each will be called exactly 1000 times.
5. Observe the `SendTransactionsProof` response contains 1000 copies of the same transaction under one `FilteredBlock`.
6. Repeat in a tight loop from a single connection to sustain amplified load; no ban is triggered because the message is well-formed and `Status::ok()` is returned.

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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L86-102)
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

            let txs: Vec<_> = txs_and_tx_indices
                .into_iter()
                .map(|(tx, _)| tx.data())
                .collect();
```

**File:** util/light-client-protocol-server/src/lib.rs (L81-91)
```rust
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
```

**File:** util/light-client-protocol-server/src/constant.rs (L7-7)
```rust
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/status.rs (L95-101)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        let code = self.code as u16;
        if !(400..500).contains(&code) {
            None
        } else {
            Some(constant::BAD_MESSAGE_BAN_TIME)
        }
```
