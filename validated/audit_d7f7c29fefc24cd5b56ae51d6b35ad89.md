The attack path is concrete and fully traceable. Here is the complete analysis:

---

### Title
Missing Ancestor-of-`last_block` Check in `GetTransactionsProofProcess::execute` Allows Unprivileged Peer to Trigger Unbanned `InternalError` Loop (DoS) — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

---

### Summary

`GetTransactionsProofProcess::execute` partitions requested `tx_hashes` into `found`/`missing` using only a main-chain membership check. It never verifies that a found transaction's block is an ancestor of (i.e., has `block_number ≤`) `last_block`. When `reply_proof` then calls `mmr.gen_proof(positions)` on an MMR anchored at `last_block.number() - 1`, positions derived from blocks *above* `last_block` are out-of-bounds for that MMR, causing the library to return an error. The server returns `StatusCode::InternalError` (5xx), which is **not** a ban-triggering code, so the peer is never disconnected and can repeat the attack indefinitely.

---

### Finding Description

**Step 1 — Entry point (P2P message):**
An unprivileged peer sends a `GetTransactionsProof` message with:
- `last_hash` = hash of a valid main-chain block at height N (e.g., 5)
- `tx_hashes` = hashes of transactions confirmed in main-chain blocks at heights > N (e.g., 6–10)

**Step 2 — `last_block` validation passes:** [1](#0-0) 

`last_block_hash` is on the main chain, so execution continues. `last_block` is the block at height 5.

**Step 3 — Missing ancestor check in `partition()`:** [2](#0-1) 

The predicate only checks `snapshot.is_main_chain(&tx_info.block_hash)`. Transactions from blocks 6–10 are on the main chain, so they land in `found`. There is **no check** that `tx_info.block_number <= last_block.number()`.

**Step 4 — Positions computed for out-of-range blocks:** [3](#0-2) 

`leaf_index_to_pos(block.number())` is called with block numbers 6–10, producing MMR positions that exceed the size of the MMR anchored at `last_block.number() - 1 = 4`.

**Step 5 — MMR anchored at `last_block.number() - 1`:** [4](#0-3) 

`chain_root_mmr(4)` creates an MMR with `mmr_size = leaf_index_to_mmr_size(4) = 7`, covering only blocks 0–4.

**Step 6 — `gen_proof` fails with out-of-bounds positions:** [5](#0-4) 

`mmr.gen_proof(positions_for_blocks_6_to_10)` returns an `Err` because those positions (≥ 8) exceed the MMR size of 7. The server returns `StatusCode::InternalError`.

**Step 7 — Peer is never banned:** [6](#0-5) [7](#0-6) 

`should_ban()` only triggers for 4xx codes. `InternalError` is 500, so `should_ban()` returns `None`. The peer receives only a warning log and is never disconnected or rate-limited.

---

### Impact Explanation

- The server performs non-trivial work per request: snapshot acquisition, transaction info lookups, block lookups, CBMT proof construction, and MMR operations — all before failing.
- The peer is never banned (5xx ≠ 4xx), so it can send this crafted message in a tight loop.
- This constitutes a **sustained, unbounded DoS** against the light-client server: CPU and I/O exhaustion from repeated DB lookups and MMR operations, plus warning log spam.
- No valid proof is ever sent; the light client simply receives no response.

---

### Likelihood Explanation

- Requires zero privileges: any peer that can open a light-client protocol connection can send this message.
- The crafted message is trivially constructable: pick any `last_hash` at a low height and any `tx_hashes` from blocks above it.
- No PoW, no key material, no Sybil attack required.
- The attack is repeatable without consequence to the attacker.

---

### Recommendation

In `execute()`, after populating `txs_in_blocks`, filter out any block whose `block.number() > last_block.number()` and move those transactions to `missing`. Add the check immediately after the `partition()` loop:

```rust
// After building txs_in_blocks, enforce ancestor constraint:
for tx_hash in found {
    let (tx, tx_info) = snapshot.get_transaction_with_info(&tx_hash).expect("tx exists");
    let block_number = snapshot
        .get_block_header(&tx_info.block_hash)
        .map(|h| h.number())
        .unwrap_or(u64::MAX);
    if block_number <= last_block.number() {
        txs_in_blocks.entry(tx_info.block_hash)...push(...);
    } else {
        missing.push(tx_hash); // treat as not provable under last_block
    }
}
```

The same pattern should be audited in `GetBlocksProofProcess::execute`, which has an identical structural issue. [8](#0-7) 

---

### Proof of Concept

```
State: chain mined to height 10; tx_A confirmed in block 7.

Attacker sends GetTransactionsProof {
    last_hash: hash_of_block_5,   // valid main-chain block
    tx_hashes: [tx_A_hash],       // tx in block 7 > last_block
}

Server trace:
  is_main_chain(block_5) → true
  partition: tx_A → found (block_7 is_main_chain = true)
  txs_in_blocks: { block_7_hash: [(tx_A, idx)] }
  positions: [leaf_index_to_pos(7)] = [11]
  reply_proof:
    mmr = chain_root_mmr(4)  // mmr_size = 7
    mmr.gen_proof([11])      // 11 > 7 → Err(...)
    → StatusCode::InternalError

Peer: not banned, sends again → repeat indefinitely
```

### Citations

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L44-52)
```rust
        if !snapshot.is_main_chain(&last_block_hash) {
            return self
                .protocol
                .reply_tip_state::<packed::SendTransactionsProof>(self.peer, self.nc)
                .await;
        }
        let last_block = snapshot
            .get_block(&last_block_hash)
            .expect("block should be in store");
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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L116-116)
```rust
            positions.push(leaf_index_to_pos(block.number()));
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

**File:** util/light-client-protocol-server/src/lib.rs (L199-199)
```rust
            let mmr = snapshot.chain_root_mmr(last_block.number() - 1);
```

**File:** util/light-client-protocol-server/src/lib.rs (L210-215)
```rust
                match mmr.gen_proof(items_positions) {
                    Ok(proof) => proof.proof_items().to_owned(),
                    Err(err) => {
                        let errmsg = format!("failed to generate a proof since {err:?}");
                        return StatusCode::InternalError.with_context(errmsg);
                    }
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

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L72-85)
```rust
        let (found, missing): (Vec<_>, Vec<_>) = block_hashes
            .into_iter()
            .partition(|block_hash| snapshot.is_main_chain(block_hash));

        let mut positions = Vec::with_capacity(found.len());
        let mut block_headers = Vec::with_capacity(found.len());
        let mut uncles_hash = Vec::with_capacity(found.len());
        let mut extensions = Vec::with_capacity(found.len());

        for block_hash in found {
            let header = snapshot
                .get_block_header(&block_hash)
                .expect("header should be in store");
            positions.push(leaf_index_to_pos(header.number()));
```
