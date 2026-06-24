Audit Report

## Title
Integer Overflow in `GET_LAST_STATE_PROOF_LIMIT` Guard Enables Unbounded Chain Scan DoS — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
In `GetLastStateProofProcess::execute`, the size guard at line 201 multiplies a peer-controlled `u64` cast to `usize` by `2` using plain `*`. On 64-bit targets in Rust release mode, supplying `last_n_blocks = 2^63` causes the multiplication to wrap to zero, bypassing the `GET_LAST_STATE_PROOF_LIMIT` check entirely. The server then collects every block number in the chain and performs N MMR root computations plus one O(N log N) `gen_proof` call per unauthenticated P2P message.

## Finding Description

**Root cause — line 201:** [1](#0-0) 

`last_n_blocks` is a peer-supplied `u64`. On a 64-bit target, `usize` is also 64 bits. When `last_n_blocks = 2^63`:
- `last_n_blocks as usize` = `9223372036854775808` (no truncation)
- `9223372036854775808_usize * 2` = `2^64` → **wraps to `0`** in Rust release mode

With `difficulties.len() = 0`, the guard evaluates `0 > 1000 = false` and does not reject the message. [2](#0-1) 

**Downstream consequence — lines 291–297:** [3](#0-2) 

Since no real chain has `2^63` blocks, `last_block_number - start_block_number <= 2^63` is always true. The "not enough blocks" branch is unconditionally taken, and `last_n_numbers` collects **every block number from `start_block_number` to `last_block_number`** — the entire chain.

**Work performed per block — `complete_headers` (lines 132–163):** [4](#0-3) 

For each of the N collected block numbers, the server calls `snapshot.get_ancestor` (chain traversal) and `snapshot.chain_root_mmr(*number - 1).get_root()` (O(log N) MMR root computation per block).

**Final `reply_proof` call:** [5](#0-4) 

`items_positions` contains N entries (one per chain block). `gen_proof` on N positions over an MMR of size N is O(N log N). There is no secondary length guard on `items_positions.len()` in `reply_proof`.

The `reorg_last_n_numbers` path at lines 237–247 is avoided entirely by setting `start_block_number = 0`, which takes the `Vec::new()` branch. [6](#0-5) 

## Impact Explanation

A single malicious peer can force the full node to allocate a `Vec` of N block numbers (entire chain), perform N MMR `get_root()` calls (each reading O(log N) DB nodes), and perform one `gen_proof` call over N positions. For a node with 100,000 blocks this is ~100,000 DB reads plus a large MMR proof computation per message. Repeated requests cause CPU exhaustion and/or OOM, crashing the node. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

- The attacker needs only a valid tip block hash (publicly observable) and the genesis hash.
- No PoW, no key, no privileged role is required — any peer can send this message.
- The molecule encoding of `Uint64 = 2^63` is a standard 8-byte little-endian value, trivially constructable.
- The overflow is deterministic and reproducible in any standard `cargo build --release`.
- The attack is repeatable from one or more peers simultaneously.

## Recommendation

Replace the plain `*` with a saturating multiply, or validate `last_n_blocks` against the limit before the cast:

```rust
// Option A: saturating multiply prevents wrap-to-zero
if self.message.difficulties().len()
    + (last_n_blocks as usize).saturating_mul(2)
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}

// Option B: early rejection before any cast
if last_n_blocks > (constant::GET_LAST_STATE_PROOF_LIMIT / 2) as u64 {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Additionally, add a secondary length guard in `reply_proof` or `complete_headers` as defense-in-depth.

## Proof of Concept

```rust
// Verify the overflow in Rust release mode:
let last_n_blocks: u64 = 1u64 << 63;
let x: usize = (last_n_blocks as usize).wrapping_mul(2);
assert_eq!(x, 0); // true on 64-bit in release mode

// Craft the malicious message:
// last_n_blocks       = 0x8000000000000000 (little-endian 8 bytes)
// difficulties        = [] (empty vector)
// last_hash           = <any valid main-chain tip hash>
// start_number        = 0
// start_hash          = <genesis hash>
// difficulty_boundary = <any U256>
//
// Expected server behavior on a node with N=100_000 blocks:
// 1. Limit check: 0 + 0 = 0 > 1000 → false (bypassed)
// 2. last_n_numbers = (0..100_000).collect() → 100_000 entries
// 3. complete_headers: 100_000 × (get_ancestor + chain_root_mmr.get_root())
// 4. reply_proof: gen_proof(100_000 positions) → CPU exhaustion / OOM
```

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L132-163)
```rust
        for number in numbers {
            if let Some(ancestor_header) = self.snapshot.get_ancestor(last_hash, *number) {
                let position = leaf_index_to_pos(*number);
                positions.push(position);

                let ancestor_block = self
                    .snapshot
                    .get_block(&ancestor_header.hash())
                    .ok_or_else(|| {
                        format!(
                            "failed to find block for header#{} (hash: {:#x})",
                            number,
                            ancestor_header.hash()
                        )
                    })?;
                let uncles_hash = ancestor_block.calc_uncles_hash();
                let extension = ancestor_block.extension();

                let parent_chain_root = if *number == 0 {
                    Default::default()
                } else {
                    let mmr = self.snapshot.chain_root_mmr(*number - 1);
                    match mmr.get_root() {
                        Ok(root) => root,
                        Err(err) => {
                            let errmsg = format!(
                                "failed to generate a root for block#{number} since {err:?}"
                            );
                            return Err(errmsg);
                        }
                    }
                };
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L199-205)
```rust
        let last_n_blocks: u64 = self.message.last_n_blocks().into();

        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L237-247)
```rust
        let reorg_last_n_numbers = if start_block_number == 0
            || snapshot
                .get_ancestor(&last_block_hash, start_block_number)
                .map(|header| header.hash() == start_block_hash)
                .unwrap_or(false)
        {
            Vec::new()
        } else {
            let min_block_number = start_block_number - min(start_block_number, last_n_blocks);
            (min_block_number..start_block_number).collect()
        };
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L291-297)
```rust
        let (sampled_numbers, last_n_numbers) = if last_block_number - start_block_number
            <= last_n_blocks
        {
            // There is not enough blocks, so we take all of them; so there is no sampled blocks.
            let sampled_numbers = Vec::new();
            let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
            (sampled_numbers, last_n_numbers)
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/lib.rs (L207-216)
```rust
            let proof = if items_positions.is_empty() {
                Default::default()
            } else {
                match mmr.gen_proof(items_positions) {
                    Ok(proof) => proof.proof_items().to_owned(),
                    Err(err) => {
                        let errmsg = format!("failed to generate a proof since {err:?}");
                        return StatusCode::InternalError.with_context(errmsg);
                    }
                }
```
