### Title
Unauthenticated Peer Can Trigger Unbounded `InternalError` via `GetBlocksProof` with `last_hash` Below Requested Block Heights — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

---

### Summary

`GetBlocksProofProcess::execute()` partitions requested block hashes into `found`/`missing` using only `snapshot.is_main_chain()`, with no check that found blocks have `header.number() <= last_block.number()`. When a peer sends `last_hash` pointing to block K and `block_hashes` containing valid main-chain blocks at heights > K, the server computes MMR positions for those out-of-range blocks and passes them to `reply_proof()`, which creates an MMR anchored at `K-1`. `mmr.gen_proof(positions)` then fails because the positions exceed the MMR's size, returning `StatusCode::InternalError`. Since `InternalError` is a 5xx code, no ban is issued, and the peer can repeat indefinitely.

---

### Finding Description

**Step 1 — Entry point**: Any unprivileged remote peer connected via the light-client P2P protocol can send a `GetBlocksProof` message (schema: `last_hash: Byte32`, `block_hashes: Byte32Vec`).

**Step 2 — `execute()` validation gap**: [1](#0-0) 

The only check on `last_hash` is `is_main_chain()`. If block K is on the main chain, `last_block` is set to block K. [2](#0-1) 

The partition into `found`/`missing` uses only `is_main_chain()` — there is **no check** that `header.number() <= last_block.number()`. Blocks at heights K+1 … N are all on the main chain and are classified as `found`.

**Step 3 — Positions computed for out-of-range blocks**: [3](#0-2) 

`leaf_index_to_pos(header.number())` is called for every found block, including those at heights > K. These positions exceed the MMR size that will be used in `reply_proof()`.

**Step 4 — MMR anchored at K-1, proof generation fails**: [4](#0-3) 

`chain_root_mmr(last_block.number() - 1)` creates an MMR of size `leaf_index_to_mmr_size(K-1)`, covering only blocks 0…K-1. Calling `mmr.gen_proof(positions)` with positions for blocks > K exceeds this MMR's size, causing the MMR library to return an error. The error is caught and returned as `StatusCode::InternalError`.

**Step 5 — No ban, indefinitely repeatable**: [5](#0-4) 

`should_ban()` only bans 4xx codes. `InternalError` is 500, which only triggers a warning log. The peer is never banned and can repeat the attack indefinitely.

---

### Impact Explanation

Each crafted request forces the server to:
1. Perform multiple DB lookups (block headers, uncles, extensions) for each "found" block
2. Construct an MMR object
3. Attempt `gen_proof()` which fails

All work is wasted. With `GET_BLOCKS_PROOF_LIMIT` blocks per request and no rate-limiting or ban, a single peer can sustain a high-frequency stream of these requests, causing repeated DB I/O and MMR computation overhead — a DoS amplification against the light-client server.

---

### Likelihood Explanation

The exploit requires only:
- Knowledge of any two main-chain block hashes where `hash_A` is at height K and `hash_B` is at height > K
- A single P2P connection to the light-client server

Both are trivially obtainable from public chain data. No PoW, no key, no privileged access required. The attack is deterministic and repeatable without consequence to the attacker.

---

### Recommendation

In `execute()`, after partitioning into `found`/`missing`, filter out any found block whose `header.number() > last_block.number()` and move it to `missing` (or return `MalformedProtocolMessage` to trigger a ban). The invariant that all proved blocks must be at or below `last_block.number()` should be enforced before computing positions:

```rust
// After the partition, add:
let (found, extra_missing): (Vec<_>, Vec<_>) = found
    .into_iter()
    .partition(|hash| {
        snapshot.get_block_header(hash)
            .map(|h| h.number() <= last_block.number())
            .unwrap_or(false)
    });
// extend missing with extra_missing, or return MalformedProtocolMessage
```

Alternatively, treat blocks with `header.number() > last_block.number()` as a `MalformedProtocolMessage` (4xx), which would trigger a ban and deter repeat abuse.

---

### Proof of Concept

```
1. Server has chain of 100 blocks (heights 0..100).
2. Attacker connects as a light-client peer.
3. Attacker sends GetBlocksProof:
     last_hash   = hash of block at height 50
     block_hashes = [hash of block 75, hash of block 90]
4. execute():
   - is_main_chain(block_50_hash) → true → last_block = block_50
   - is_main_chain(block_75_hash) → true → found
   - is_main_chain(block_90_hash) → true → found
   - positions = [leaf_index_to_pos(75), leaf_index_to_pos(90)]
5. reply_proof():
   - mmr = chain_root_mmr(49)  // size covers only blocks 0..49
   - mmr.gen_proof([pos(75), pos(90)]) → Err(...)
   - returns StatusCode::InternalError
6. should_ban() → None (500 is not in 400..500)
7. Attacker repeats from step 3 indefinitely.
```

### Citations

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L44-50)
```rust
        let last_block_hash = self.message.last_hash().to_entity();
        if !snapshot.is_main_chain(&last_block_hash) {
            return self
                .protocol
                .reply_tip_state::<packed::SendBlocksProof>(self.peer, self.nc)
                .await;
        }
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L72-74)
```rust
        let (found, missing): (Vec<_>, Vec<_>) = block_hashes
            .into_iter()
            .partition(|block_hash| snapshot.is_main_chain(block_hash));
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L81-85)
```rust
        for block_hash in found {
            let header = snapshot
                .get_block_header(&block_hash)
                .expect("header should be in store");
            positions.push(leaf_index_to_pos(header.number()));
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

**File:** util/light-client-protocol-server/src/status.rs (L95-108)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        let code = self.code as u16;
        if !(400..500).contains(&code) {
            None
        } else {
            Some(constant::BAD_MESSAGE_BAN_TIME)
        }
    }

    /// Whether a warning log should be output.
    pub fn should_warn(&self) -> bool {
        let code = self.code as u16;
        (500..600).contains(&code)
    }
```
