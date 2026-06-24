Audit Report

## Title
Integer Overflow in `last_n_blocks` Limit Check Bypasses 1000-Sample Cap, Enabling O(chain-height) OOM — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary

In `GetLastStateProofProcess::execute`, the guard at line 201 computes `(last_n_blocks as usize) * 2` without overflow protection. On a 64-bit release build, sending `last_n_blocks = 2^63` causes this multiplication to wrap to `0`, making the guard evaluate to `0 > 1000` (false), silently bypassing the 1000-sample cap. Downstream, the "not enough blocks" branch at line 291–296 then collects every block number from `start_block_number` to `last_block_number` into a `Vec<u64>`, followed by one `get_ancestor` + one MMR root DB read per entry in `complete_headers`. On a chain of N blocks this is O(N) heap allocation and O(N) blocking DB I/O, sufficient to OOM-crash the node.

## Finding Description

**Root cause — unchecked multiplication at line 201:**

```rust
// util/light-client-protocol-server/src/components/get_last_state_proof.rs, L201-205
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // 1000
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

With `last_n_blocks = 2^63 = 9223372036854775808`:
- `last_n_blocks as usize` = `9223372036854775808` (valid on 64-bit)
- `9223372036854775808_usize * 2` in release mode (wrapping) = `0`
- `0 + 0 > 1000` → **false** → guard is bypassed

In debug mode Rust panics on overflow; in release mode it silently wraps. `GET_LAST_STATE_PROOF_LIMIT` is `1000`.

**Exploit preconditions (all trivially satisfiable):**
- `last_hash`: any valid main-chain block hash (obtainable from `SendLastState`)
- `start_hash`: canonical block hash at height 1 (or set `start_number = 0` to skip the reorg branch entirely)
- `start_number = 1` (or 0)
- `difficulties = []` — empty vec passes all three difficulty validation checks (lines 254, 259, 268)
- `difficulty_boundary = U256::MAX`
- `last_n_blocks = 1u64 << 63`

**Exploit flow:**

1. Guard at L201 wraps to `0 > 1000` → false → no early return.
2. With `start_hash` matching canonical block at `start_number`, `reorg_last_n_numbers = Vec::new()` (harmless).
3. Line 291: `last_block_number - start_block_number <= last_n_blocks` → `(N-1) <= 2^63` → **true** for any realistic chain height.
4. Line 296: `(start_block_number..last_block_number).collect::<Vec<_>>()` allocates a `Vec<u64>` of `N-1` entries — **O(chain_height) heap allocation**.
5. `complete_headers` (L132–180) iterates every entry: one `get_ancestor` DB walk + one `chain_root_mmr` computation per block — **O(chain_height) blocking DB I/O** on the async executor thread.

**Why existing checks are insufficient:**

The only size guard is the overflowing multiplication at L201. There is no independent upper-bound check on the raw `last_n_blocks` u64 value before arithmetic is performed. The molecule/packed encoding accepts any u64, so `2^63` is a valid wire value.

## Impact Explanation

A single malformed `GetLastStateProof` message causes the full-node light-client server to allocate a `Vec<u64>` proportional to the entire chain height and then perform one DB read per entry. On mainnet (millions of blocks) this exhausts heap memory and crashes the node process (OOM). This matches the allowed bounty impact: **High — Vulnerabilities which could easily crash a CKB node** (10001–15000 points).

## Likelihood Explanation

- The light-client protocol is opt-in but enabled in production deployments.
- The attacker requires only a valid main-chain block hash, obtainable from any `SendLastState` message — no PoW, no stake, no privileged role.
- The overflow value `2^63` is a single fixed constant; no brute-force or fuzzing is needed.
- The attack is repeatable: the node crashes and can be re-targeted on restart.
- Empty `difficulties` and `U256::MAX` boundary are always accepted by the existing validation logic.

## Recommendation

Replace the unchecked multiplication with saturating arithmetic before the comparison:

```rust
let sample_count = (last_n_blocks as usize)
    .saturating_mul(2)
    .saturating_add(self.message.difficulties().len());
if sample_count > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Additionally, add an explicit early rejection on the raw value before any arithmetic:

```rust
if last_n_blocks > constant::GET_LAST_STATE_PROOF_LIMIT as u64 {
    return StatusCode::MalformedProtocolMessage.with_context("last_n_blocks too large");
}
```

## Proof of Concept

```rust
// Minimal PoC against a node with light-client enabled and N blocks in chain
let msg = GetLastStateProof {
    last_hash:          <any valid main-chain tip hash from SendLastState>,
    start_hash:         <canonical block hash at height 1>,
    start_number:       1u64,
    last_n_blocks:      1u64 << 63,   // 2^63 — wraps limit check to 0 in release
    difficulty_boundary: U256::MAX,
    difficulties:       vec![],        // passes all three difficulty guards
};
// Release build: server enters (1..N).collect() → OOM crash
// Fixed build:   saturating_mul(2) = usize::MAX → guard fires immediately
```

**Verification steps:**
1. Build `ckb` in release mode with light-client server enabled.
2. Start a node synced to at least several thousand blocks.
3. Connect as a light-client peer, complete the handshake.
4. Send the crafted `GetLastStateProof` message above.
5. Observe: server process OOM-kills or RSS grows proportionally to chain height; no `MalformedProtocolMessage` is returned. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L132-135)
```rust
        for number in numbers {
            if let Some(ancestor_header) = self.snapshot.get_ancestor(last_hash, *number) {
                let position = leaf_index_to_pos(*number);
                positions.push(position);
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L201-205)
```rust
        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
        }
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
