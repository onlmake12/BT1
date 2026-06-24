Audit Report

## Title
Missing Ancestor-of-`last_block` Height Check in `GetTransactionsProofProcess::execute` Allows Unbanned Peer to Trigger Repeated `InternalError` (DoS) — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

## Summary

`GetTransactionsProofProcess::execute` partitions requested `tx_hashes` into `found`/`missing` using only a main-chain membership check, with no verification that a found transaction's block is at or below `last_block`'s height. When `reply_proof` calls `mmr.gen_proof(positions)` on an MMR anchored at `last_block.number() - 1`, positions derived from blocks above `last_block` are out-of-bounds, causing the library to return an error and the server to return `StatusCode::InternalError` (500). Because `should_ban()` only triggers for 4xx codes, the peer is never disconnected and can repeat the attack indefinitely at zero cost.

## Finding Description

**Root cause — missing height guard in `partition()`:** [1](#0-0) 

The predicate only calls `snapshot.is_main_chain(&tx_info.block_hash)`. A transaction confirmed in a main-chain block at height N+k (where k > 0) passes this check even when `last_block` is at height N. Those transactions land in `found` and their block numbers are used to compute MMR leaf positions: [2](#0-1) 

**MMR is anchored at `last_block.number() - 1`:** [3](#0-2) 

This MMR covers only blocks 0 through `last_block.number() - 1`. Positions derived from blocks above `last_block` exceed the MMR size, causing `gen_proof` to return `Err`: [4](#0-3) 

**Peer is never banned:**

`should_ban()` only returns `Some(ban_time)` for codes in `400..500`. `InternalError = 500` falls outside that range: [5](#0-4) 

The handler logs a warning and continues; the peer is never disconnected: [6](#0-5) 

**Identical structural issue in `GetBlocksProofProcess::execute`:** [7](#0-6) 

The same `is_main_chain`-only partition with no height bound against `last_block.number()` exists here.

## Impact Explanation

Each crafted request forces the server to perform snapshot acquisition, transaction-info lookups, block fetches, CBMT proof construction, and MMR operations before failing. Because the peer is never banned, it can send this message in a tight loop. This constitutes a sustained, low-cost resource exhaustion attack against the light-client server component of the CKB node, matching **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs** (10001–15000 points).

## Likelihood Explanation

- Requires zero privileges: any peer that can open a light-client protocol connection can send `GetTransactionsProof`.
- The crafted message is trivially constructable: pick any valid main-chain `last_hash` at a low height and any `tx_hashes` from blocks above it.
- No PoW, key material, or Sybil attack required.
- The attack is repeatable without consequence to the attacker (no ban, no rate limit).

## Recommendation

After the `partition()` loop and after building `txs_in_blocks`, filter out any entry whose block number exceeds `last_block.number()` and move those `tx_hashes` to `missing`:

```rust
txs_in_blocks.retain(|block_hash, _| {
    snapshot
        .get_block_number(block_hash)
        .map(|n| n <= last_block.number())
        .unwrap_or(false)
});
```

Transactions whose blocks were removed should be appended to `missing`. Apply the same fix to `GetBlocksProofProcess::execute` by filtering `found` block hashes against `last_block.number()` before computing positions.

## Proof of Concept

```
Precondition: chain mined to height 10; tx_A confirmed in block 7.

Attacker sends GetTransactionsProof {
    last_hash: hash_of_block_5,   // valid main-chain block at height 5
    tx_hashes: [tx_A_hash],       // tx_A is in block 7 > last_block height
}

Server trace:
  is_main_chain(block_5_hash) → true          // last_block = block at height 5
  partition: tx_A → found                     // block_7 is_main_chain = true (no height check)
  txs_in_blocks: { block_7_hash: [(tx_A, 0)] }
  positions: [leaf_index_to_pos(7)] = [11]
  reply_proof:
    mmr = chain_root_mmr(4)                   // mmr_size = leaf_index_to_mmr_size(4) = 7
    mmr.gen_proof([11])                       // 11 >= 7 → Err(...)
    → StatusCode::InternalError (500)
  should_ban(500) → None                      // peer NOT banned
  → warn log only

Attacker: sends again → repeat indefinitely
```

### Citations

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
