The code is concrete and the path is fully traceable. Here is the analysis:

**Code path confirmed:**

1. `execute()` checks `len() > 1000` at line 37 — this only bounds the raw count, not uniqueness.
2. The `partition` at lines 54–64 iterates every element including duplicates, calling `snapshot.get_transaction_info(tx_hash)` once per entry — 1000 DB reads for the same hash.
3. The `for tx_hash in found` loop at lines 67–75 calls `snapshot.get_transaction_with_info(&tx_hash)` once per entry — another 1000 DB reads for the same hash.
4. Because all duplicates share the same `tx_info.block_hash`, the `txs_in_blocks` HashMap ends up with one block entry containing a `Vec` of 1000 identical `(tx, index)` pairs.
5. `CBMT::build_merkle_proof` at line 86 is called with a `Vec` of 1000 identical `u32` indices.

There is no deduplication anywhere in this path.

---

### Title
Missing tx_hash deduplication in `GetTransactionsProofProcess::execute` enables CPU/DB amplification DoS — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

### Summary
An unprivileged remote peer can send a `GetTransactionsProof` message containing 1000 identical `tx_hash` values. The server's length guard passes (1000 ≤ 1000), then performs 2000 redundant database lookups and invokes `CBMT::build_merkle_proof` with 1000 identical indices, all for a single known transaction. The attacker's cost is negligible; the server's cost scales linearly with the duplicate count.

### Finding Description
`GetTransactionsProofProcess::execute` enforces a count limit but never deduplicates the input `tx_hashes` vector before processing. [1](#0-0) 

The limit check at line 37 only prevents messages longer than 1000 entries; it does not prevent 1000 entries that are all the same hash. The subsequent `partition` call iterates all 1000 entries: [2](#0-1) 

Each iteration issues a `get_transaction_info` DB read. Then the `found` loop issues a second `get_transaction_with_info` DB read per entry: [3](#0-2) 

Because all duplicates resolve to the same block hash, the `txs_in_blocks` HashMap accumulates a single block entry with 1000 identical `(tx, index)` pairs. `CBMT::build_merkle_proof` is then called with a 1000-element vector of identical indices: [4](#0-3) 

The constant is defined as: [5](#0-4) 

### Impact Explanation
Each malicious request causes:
- 2000 RocksDB point-reads (1000 × `get_transaction_info` + 1000 × `get_transaction_with_info`) for data that could be fetched once.
- One `CBMT::build_merkle_proof` call with 1000 identical indices, whose internal work depends on whether the CBMT implementation deduplicates — if it does not, this is O(n log n) in the duplicate count.
- A proportionally large `FilteredBlock` response with 1000 duplicate transaction entries, consuming additional serialization CPU and network bandwidth.

An attacker who knows a single valid `tx_hash` (trivially obtained from any block explorer or by observing the P2P network) can sustain this at the rate the network allows new connections, with no PoW or stake requirement.

### Likelihood Explanation
- Precondition: light-client protocol enabled on the target node. This is an opt-in feature but is the entire purpose of the `light-client-protocol-server` crate.
- Attacker knowledge required: one valid confirmed `tx_hash` — public information.
- No authentication, no rate-limiting, no ban triggered (the response is `Status::ok()`). [6](#0-5) 

`MalformedProtocolMessage` would trigger a ban, but this path never returns that status for duplicate hashes — it processes them and returns `ok`.

### Recommendation
Deduplicate `tx_hashes` immediately after the length check, before any DB access:

```rust
// After the length check at line 39:
let mut tx_hashes: Vec<_> = self.message.tx_hashes().to_entity().into_iter().collect();
tx_hashes.sort_unstable();
tx_hashes.dedup();
if tx_hashes.is_empty() {
    return StatusCode::MalformedProtocolMessage.with_context("no transaction after dedup");
}
```

Alternatively, reject the message with `MalformedProtocolMessage` if any duplicate is detected, which also bans the peer. [7](#0-6) 

### Proof of Concept
Send a `GetTransactionsProof` message where `tx_hashes` is a vector of 1000 copies of any known confirmed transaction hash, with `last_hash` set to the current chain tip. The server will:
1. Pass the length check (1000 ≤ 1000).
2. Issue 2000 DB reads for the same row.
3. Call `CBMT::build_merkle_proof` with `[index; 1000]`.
4. Return `Status::ok()` — no ban, no rate limit, repeatable indefinitely.

A unit test mirroring the existing `get_transactions_proof_with_missing_txs` test but with `tx_hashes` set to `vec![tx1.hash(); 1000]` would reproduce the issue and can measure CPU time to confirm amplification. [8](#0-7)

### Citations

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L33-39)
```rust
        if self.message.tx_hashes().is_empty() {
            return StatusCode::MalformedProtocolMessage.with_context("no transaction");
        }

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

**File:** util/light-client-protocol-server/src/constant.rs (L7-7)
```rust
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/lib.rs (L80-92)
```rust
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

**File:** util/light-client-protocol-server/src/tests/components/get_transactions_proof.rs (L84-93)
```rust
    let data = {
        let content = packed::GetTransactionsProof::new_builder()
            .last_hash(snapshot.tip_header().hash())
            .tx_hashes(vec![tx1.hash(), tx2.hash(), tx3_hash.into()])
            .build();
        packed::LightClientMessage::new_builder()
            .set(content)
            .build()
    }
    .as_bytes();
```
