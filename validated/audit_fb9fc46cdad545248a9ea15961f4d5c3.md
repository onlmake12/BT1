All five cited code references are confirmed in the actual source. Let me verify the complete exploit chain:

- **[1] Lines 72-74**: Partition uses only `is_main_chain`, no height bound check. [1](#0-0) 
- **[2] Lines 81-85**: Positions computed for all found blocks regardless of height vs `last_block`. [2](#0-1) 
- **[3] Lines 199-215**: MMR anchored at `last_block.number()-1`; out-of-range `gen_proof` returns `InternalError`. [3](#0-2) 
- **[4] Lines 95-101**: `should_ban()` only covers 400–499; code 500 is never banned. [4](#0-3) 
- **[5] Line 5**: `GET_BLOCKS_PROOF_LIMIT = 1000`. [5](#0-4) 

---

Audit Report

## Title
Unauthenticated Peer Can Trigger Unbounded Wasted Work via `GetBlocksProof` with `last_hash` Below Requested Block Heights — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

## Summary
`GetBlocksProofProcess::execute()` partitions requested block hashes into `found`/`missing` using only `snapshot.is_main_chain()`, with no check that found blocks satisfy `header.number() <= last_block.number()`. When a peer sends a `last_hash` at height K and `block_hashes` containing valid main-chain blocks at heights > K, the server performs up to 1000 DB lookups, constructs an MMR anchored at K−1, and calls `gen_proof()` with out-of-range positions, which fails and returns `StatusCode::InternalError` (500). Because `should_ban()` only covers 400–499, no ban is issued, and the peer can repeat indefinitely at zero cost.

## Finding Description
**Root cause**: `execute()` validates `last_hash` only for main-chain membership (lines 44–50), then partitions `block_hashes` by `is_main_chain()` alone (lines 72–74). There is no guard ensuring `header.number() <= last_block.number()` for any found block.

**Exploit flow**:
1. Attacker sends `GetBlocksProof { last_hash: hash_K, block_hashes: [hash_{K+1}, …, hash_{K+1000}] }` where all hashes are valid main-chain blocks.
2. `execute()` sets `last_block` = block K, classifies all 1000 hashes as `found`.
3. For each found block, the server fetches the header, uncles, and extension from the DB (lines 81–94), then pushes `leaf_index_to_pos(header.number())` — positions for blocks K+1…K+1000.
4. `reply_proof()` creates `chain_root_mmr(K−1)`, whose size covers only blocks 0…K−1. `mmr.gen_proof(positions)` receives positions exceeding the MMR's size, returns `Err`, and the function returns `StatusCode::InternalError` (lines 199–215 of `lib.rs`).
5. `should_ban()` returns `None` for code 500 (lines 95–101 of `status.rs`). Only a warning log is emitted. The peer is never banned.

**Existing checks that fail**:
- The `is_empty()` and `> GET_BLOCKS_PROOF_LIMIT` guards (lines 34–40) bound the count but not the height relationship.
- The duplicate-hash check (lines 62–70) is irrelevant to the height invariant.
- No connection-level rate limiting exists in the light-client handler.

## Impact Explanation
Each crafted request forces the server to perform up to 1000 RocksDB reads (block headers, uncle hashes, extensions), construct an MMR object, and execute a failing `gen_proof()` computation — all wasted. With `GET_BLOCKS_PROOF_LIMIT = 1000` and no ban or rate limit, a single peer can sustain a continuous stream of such requests, exhausting the node's I/O and CPU budget allocated to the light-client protocol handler. This matches **High: Vulnerabilities which could easily crash a CKB node** — a sustained attack can render the light-client server (and, through shared I/O, the broader node) unresponsive to legitimate traffic.

## Likelihood Explanation
The attacker needs only: (1) a P2P connection to a light-client-enabled CKB node, and (2) any two main-chain block hashes where one is at a lower height than the other — both trivially obtained from public chain data or a block explorer. No proof-of-work, no key material, no privileged access, and no victim mistake is required. The attack is deterministic, repeatable without consequence, and requires minimal bandwidth.

## Recommendation
In `execute()`, after the `found`/`missing` partition, filter out any found block whose `header.number() > last_block.number()` before computing positions. The cleanest fix is to treat such blocks as a `MalformedProtocolMessage` (4xx), which triggers a ban and deters repeat abuse:

```rust
// After partition (line 74), before the position-computation loop:
for block_hash in &found {
    let header = snapshot
        .get_block_header(block_hash)
        .expect("header should be in store");
    if header.number() > last_block.number() {
        return StatusCode::MalformedProtocolMessage
            .with_context("block hash is beyond last_hash height");
    }
}
```

Alternatively, silently move out-of-range found blocks to `missing`, but returning `MalformedProtocolMessage` is preferable because it bans the offending peer.

## Proof of Concept
```
Setup: CKB node with light-client server enabled, chain at height 100.

1. Attacker connects as a light-client peer.
2. Attacker sends GetBlocksProof:
     last_hash    = hash of block at height 50
     block_hashes = [hash(block_51), hash(block_52), …, hash(block_100)]  // 50 entries, all main-chain
3. Server execute():
   - is_main_chain(hash_50) → true → last_block = block_50
   - all 50 hashes → is_main_chain() → true → found
   - For each: fetches header + uncles + extension from DB
   - positions = [leaf_index_to_pos(51), …, leaf_index_to_pos(100)]
4. reply_proof():
   - mmr = chain_root_mmr(49)  // covers only blocks 0..49
   - mmr.gen_proof([pos(51)..pos(100)]) → Err(...)
   - returns StatusCode::InternalError (500)
5. should_ban() → None  (500 ∉ 400..500)
6. Attacker repeats step 2 in a tight loop indefinitely.

Expected observable effect: node I/O and CPU spike; light-client responses to
legitimate peers are delayed or dropped.
```

### Citations

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

**File:** util/light-client-protocol-server/src/constant.rs (L5-5)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
```
