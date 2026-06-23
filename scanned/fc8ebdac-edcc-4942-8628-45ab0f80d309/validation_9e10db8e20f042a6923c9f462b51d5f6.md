Based on my analysis of the actual code, here is my assessment:

---

### Title
Unbounded Repeated CPU/IO Amplification via Duplicate `tx_hashes` in `GetTransactionsProof` — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

### Summary

An unprivileged remote peer can send a `GetTransactionsProof` message containing up to 1000 copies of the **same** transaction hash (or 1000 distinct hashes from the same block). The server performs 1000 individual DB reads and calls `CBMT::build_merkle_proof` with 1000 indices, with no deduplication, no per-peer rate limiting, and no ban for valid-but-expensive requests. This can be repeated indefinitely.

### Finding Description

The `execute()` function in `GetTransactionsProofProcess` enforces only a single bound: [1](#0-0) 

`GET_TRANSACTIONS_PROOF_LIMIT` is set to 1000: [2](#0-1) 

After this check, the code iterates over all `tx_hashes` without deduplication, calling `get_transaction_with_info` for each one: [3](#0-2) 

Then for each unique block encountered, it calls `CBMT::build_merkle_proof` with all collected indices: [4](#0-3) 

**The stronger attack vector (no 1000-tx block required):** An attacker sends 1000 copies of the same single confirmed transaction hash. This triggers:
- 1000 DB reads for the same transaction record
- `CBMT::build_merkle_proof` called with 1000 duplicate indices (O(N log N) work)
- A full block load from storage

This requires only that **any** confirmed transaction exists on the main chain — a trivially satisfied precondition on any running node.

### Impact Explanation

The server performs O(N) DB reads and O(N log N) CBMT work per request, where N ≤ 1000. A peer can send these requests in rapid succession. The `MalformedProtocolMessage` (400-range) status triggers a ban, but a valid request (≤1000 hashes, valid `last_hash`) returns `OK` or a proof response — **no ban is applied**: [5](#0-4) 

There is no per-peer request rate limiting in the light client protocol server for this message type. A single attacker connection can saturate the server's I/O and CPU with repeated maximum-cost requests.

### Likelihood Explanation

- Precondition: any confirmed transaction on the main chain (always true)
- Attacker capability: any unprivileged P2P peer connecting to a light-client-enabled node
- No PoW, no key, no privileged role required
- Locally testable with a single connection

### Recommendation

1. **Deduplicate `tx_hashes`** before processing (convert to a `HashSet` first).
2. **Add per-peer rate limiting** for `GetTransactionsProof` messages (e.g., token bucket or cooldown window).
3. Consider reducing `GET_TRANSACTIONS_PROOF_LIMIT` or adding a per-request cost budget.

### Proof of Concept

```
1. Start a CKB node with light client protocol enabled.
2. Find any confirmed tx_hash H on the main chain.
3. Construct: GetTransactionsProof { last_hash: tip_hash, tx_hashes: [H; 1000] }
4. Send repeatedly over P2P — no ban is applied, server processes each request fully.
5. Observe: 1000 DB reads + O(1000 log 1000) CBMT work per message, unbounded repetition.
``` [6](#0-5)

### Citations

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L37-39)
```rust
        if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
        }
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L54-97)
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

        let mut positions = Vec::with_capacity(txs_in_blocks.len());
        let mut filtered_blocks = Vec::with_capacity(txs_in_blocks.len());
        let mut uncles_hash = Vec::with_capacity(txs_in_blocks.len());
        let mut extensions = Vec::with_capacity(txs_in_blocks.len());

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
```

**File:** util/light-client-protocol-server/src/constant.rs (L7-7)
```rust
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/status.rs (L95-102)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        let code = self.code as u16;
        if !(400..500).contains(&code) {
            None
        } else {
            Some(constant::BAD_MESSAGE_BAN_TIME)
        }
    }
```
