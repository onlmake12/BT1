The vulnerability claim is concrete and traceable. Here is the full analysis:

---

### Title
Missing ancestor-height check in `GetBlocksProofProcess::execute` allows unbounded `InternalError` triggering without peer ban — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

### Summary

`GetBlocksProofProcess::execute` partitions requested `block_hashes` into `found`/`missing` using only `is_main_chain`, with no check that each found block's height is ≤ `last_hash`'s height. When a found block's height exceeds `last_block.number()`, its MMR leaf position exceeds the MMR's size, causing `gen_proof` to fail. The resulting `StatusCode::InternalError` (5xx) never triggers a peer ban, so an attacker can repeat this indefinitely.

### Finding Description

**Step 1 — Partition only checks main-chain membership, not ancestor relationship:**

In `execute`, the only filter applied to `block_hashes` before computing MMR positions is:

```rust
let (found, missing): (Vec<_>, Vec<_>) = block_hashes
    .into_iter()
    .partition(|block_hash| snapshot.is_main_chain(block_hash));
``` [1](#0-0) 

A block at height 50 that is on the main chain passes this check even when `last_hash` is block 10.

**Step 2 — MMR position is computed unconditionally from the block's own height:**

```rust
positions.push(leaf_index_to_pos(header.number()));
``` [2](#0-1) 

For block 50, this produces a position corresponding to leaf index 50.

**Step 3 — `reply_proof` builds the MMR only up to `last_block.number() - 1`:**

```rust
let mmr = snapshot.chain_root_mmr(last_block.number() - 1);
// ...
match mmr.gen_proof(items_positions) {
    Ok(proof) => proof.proof_items().to_owned(),
    Err(err) => {
        let errmsg = format!("failed to generate a proof since {err:?}");
        return StatusCode::InternalError.with_context(errmsg);
    }
}
``` [3](#0-2) 

With `last_block = block[10]`, the MMR covers only blocks 0–9. Requesting a proof for leaf index 50 is out of range, so `gen_proof` returns an error.

**Step 4 — `InternalError` (5xx) never bans the peer:**

```rust
pub fn should_ban(&self) -> Option<Duration> {
    let code = self.code as u16;
    if !(400..500).contains(&code) {
        None
    } else {
        Some(constant::BAD_MESSAGE_BAN_TIME)
    }
}
``` [4](#0-3) 

`InternalError = 500` is outside `400..500`, so `should_ban()` returns `None`. The peer receives only a `warn!` log and is never disconnected or banned. [5](#0-4) 

### Impact Explanation

An unprivileged remote peer can send an unlimited stream of `GetBlocksProof` messages where `last_hash` points to an old main-chain block and `block_hashes` contains newer main-chain blocks. Each message forces the server to perform DB lookups, compute MMR positions, and attempt (and fail) MMR proof generation — all at zero cost to the attacker because no ban is ever applied. This degrades the light client proof service for legitimate clients.

### Likelihood Explanation

The attack requires only a valid P2P connection to a CKB full node running the light client protocol server. No PoW, no keys, no privileged access. The attacker needs only to know two main-chain block hashes where one is at a higher height than the other — trivially obtained from any block explorer or by syncing a few headers.

### Recommendation

After partitioning `block_hashes` into `found`, add a height guard before computing positions:

```rust
for block_hash in found {
    let header = snapshot
        .get_block_header(&block_hash)
        .expect("header should be in store");
    if header.number() >= last_block.number() {
        // treat as missing or return MalformedProtocolMessage
        missing_items.push(block_hash);
        continue;
    }
    positions.push(leaf_index_to_pos(header.number()));
    // ...
}
```

Alternatively, return `StatusCode::MalformedProtocolMessage` (4xx, which does ban) when any found block's height ≥ `last_block.number()`, since a well-formed request must only ask for ancestors of `last_hash`.

### Proof of Concept

```rust
// mine to height 100
chain.mine_to(100);
let snapshot = chain.shared().snapshot();
let last_hash = snapshot.get_block_hash(10).unwrap();   // last_hash = block[10]
let future_hash = snapshot.get_block_hash(50).unwrap(); // block[50] > block[10]

let data = packed::GetBlocksProof::new_builder()
    .last_hash(last_hash)
    .block_hashes(vec![future_hash])
    .build();

// Send 10_000 times — peer is never banned, server returns InternalError each time
for _ in 0..10_000 {
    protocol.received(nc.context(), peer_index, data.as_bytes()).await;
}
// assert: peer is not banned
assert!(nc.not_banned(peer_index));
// assert: all responses are InternalError (no SendBlocksProof)
```

### Citations

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L72-74)
```rust
        let (found, missing): (Vec<_>, Vec<_>) = block_hashes
            .into_iter()
            .partition(|block_hash| snapshot.is_main_chain(block_hash));
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L85-85)
```rust
            positions.push(leaf_index_to_pos(header.number()));
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

**File:** util/light-client-protocol-server/src/lib.rs (L199-215)
```rust
            let mmr = snapshot.chain_root_mmr(last_block.number() - 1);
            let parent_chain_root = match mmr.get_root() {
                Ok(root) => root,
                Err(err) => {
                    let errmsg = format!("failed to generate a root since {err:?}");
                    return StatusCode::InternalError.with_context(errmsg);
                }
            };
            let proof = if items_positions.is_empty() {
                Default::default()
            } else {
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
