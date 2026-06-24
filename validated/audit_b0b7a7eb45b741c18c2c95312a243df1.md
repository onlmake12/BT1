All four code claims check out against the actual source:

1. No height guard in `execute()` — confirmed at lines 81–85 of `get_blocks_proof.rs` [1](#0-0) 
2. `reply_proof` builds MMR for `last_block.number() - 1` — confirmed at line 199 of `lib.rs` [2](#0-1) 
3. `gen_proof` failure returns `InternalError` (500) — confirmed at lines 210–215 of `lib.rs` [3](#0-2) 
4. `should_ban()` range is `400..500`, excluding 500 — confirmed at lines 95–101 of `status.rs` [4](#0-3) 

The finding is real. The severity is **Low** ("Any other important performance improvements for CKB"), not Medium — the per-request cost is minimal (fast MMR bounds failure, no full traversal), there is no amplification factor, and the "suboptimal state storage mechanism" Medium category does not apply to a protocol handler input validation gap.

---

Audit Report

## Title
Missing ancestor-height validation in `GetBlocksProofProcess::execute` allows unbanned peers to trigger repeated `InternalError` — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

## Summary
A remote peer can send a `GetBlocksProof` message where `last_hash` points to block at height N and `block_hashes` contains a hash of a block at height M > N (both on the main chain). The server accepts both as valid, computes `leaf_index_to_pos(M)`, then calls `reply_proof` with an MMR sized for N−1. `gen_proof(pos(M))` fails, returning `StatusCode::InternalError` (500). Because `should_ban()` only acts on 4xx codes, the peer is never banned and can repeat this indefinitely at negligible per-request cost.

## Finding Description
In `execute()`, `block_hashes` are partitioned by `is_main_chain()` only — there is no check that each found block's height is ≤ `last_block.number()`. For every hash in `found`, `leaf_index_to_pos(header.number())` is unconditionally appended to `positions`. `reply_proof` then constructs the MMR as `chain_root_mmr(last_block.number() - 1)`, so any position derived from a block taller than `last_block` is out of bounds. `mmr.gen_proof(items_positions)` fails and returns `StatusCode::InternalError` (500). `should_ban()` checks `!(400..500).contains(&code)`, so code 500 returns `None` — no ban is issued. The server only emits a `warn!` log and moves on.

## Impact Explanation
Each malformed request causes the server to fetch block headers and attempt (and fail) MMR proof generation without penalizing the sender. The peer can repeat this at will. The per-request cost is low (fast MMR bounds failure, no full proof traversal completes). This matches **Low (501–2000 points): Any other important performance improvements for CKB** — the fix eliminates unnecessary server work and enables proper peer penalization via the existing ban mechanism.

## Likelihood Explanation
Any unprivileged P2P peer can craft this message. No PoW, key material, or privileged access is required. The condition (two main-chain blocks where one is taller than the other) is trivially satisfiable on any live chain. The attack is freely repeatable with no cost to the attacker.

## Recommendation
Add a height guard in `execute()` immediately after fetching the header, before appending to `positions`:

```rust
if header.number() > last_block.number() {
    return StatusCode::MalformedProtocolMessage
        .with_context("block height exceeds last_hash block");
}
```

This converts the path to a 4xx status, which triggers `should_ban()` and penalizes the peer.

## Proof of Concept
Build a chain of height 10. Send `GetBlocksProof` with `last_hash = block[5].hash()` and `block_hashes = [block[8].hash()]`. Assert the response status is `InternalError` (500) and that `nc.ban_peer` is **not** called. This confirms the missing height validation and the missing ban. A unit test using the existing mock `nc` infrastructure in `util/light-client-protocol-server/src/tests/` can reproduce this deterministically.

### Citations

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L81-85)
```rust
        for block_hash in found {
            let header = snapshot
                .get_block_header(&block_hash)
                .expect("header should be in store");
            positions.push(leaf_index_to_pos(header.number()));
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
