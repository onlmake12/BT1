Audit Report

## Title
Integer Overflow in `GetLastStateProof` Limit Guard Enables Unbounded Allocation DoS — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary

The limit guard at line 201 of `get_last_state_proof.rs` performs an unchecked multiplication `(last_n_blocks as usize) * 2` that wraps to zero in release builds when `last_n_blocks = 2^63`, bypassing `GET_LAST_STATE_PROOF_LIMIT` entirely. The subsequent reorg and `last_n_numbers` branches then allocate Vecs proportional to the full chain height with no independent cap, and `complete_headers` performs a DB lookup and MMR root computation for every entry — causing OOM and crashing the full-node process from a single unauthenticated P2P message.

## Finding Description

**Root cause — overflow in the limit guard (lines 199–205):**

```rust
let last_n_blocks: u64 = self.message.last_n_blocks().into();

if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

With `last_n_blocks = 2^63` on a 64-bit target:
- `last_n_blocks as usize = 2^63` (no truncation; `usize` is 64-bit)
- `(2^63_usize) * 2` wraps to **0** in Rust release mode (two's-complement, no overflow check)
- Guard evaluates to `0 + 0 > 1000` → **false** → passes

`GET_LAST_STATE_PROOF_LIMIT` is defined as `1000` at `constant.rs:6`.

**Unbounded `reorg_last_n_numbers` allocation (lines 244–246):**

```rust
let min_block_number = start_block_number - min(start_block_number, last_n_blocks);
(min_block_number..start_block_number).collect()
```

With `last_n_blocks = 2^63` and `start_block_number = S`:
- `min(S, 2^63) = S` → `min_block_number = 0`
- Allocates `(0..S).collect()` — up to `S` entries, entirely uncapped by `GET_LAST_STATE_PROOF_LIMIT`

The only precondition for this branch is that `start_hash` does not match the real ancestor at `start_block_number`, which any random 32-byte value satisfies.

**Unbounded `last_n_numbers` allocation (lines 291–296):**

```rust
let (sampled_numbers, last_n_numbers) = if last_block_number - start_block_number
    <= last_n_blocks
{
    let sampled_numbers = Vec::new();
    let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
```

With `last_n_blocks = 2^63`, the condition `last_block_number - start_block_number <= 2^63` is always true for any realistic chain height, so this branch is always taken, allocating `last_block_number - start_block_number` entries with no cap.

**`complete_headers` amplifies the damage (lines 350–365):**

```rust
let block_numbers = reorg_last_n_numbers
    .into_iter()
    .chain(sampled_numbers)
    .chain(last_n_numbers)
    .collect::<Vec<_>>();
// ...
sampler.complete_headers(&mut positions, &last_block_hash, &block_numbers)
```

`complete_headers` calls `get_ancestor`, `get_block`, `calc_uncles_hash`, and `chain_root_mmr(...).get_root()` for **every** entry in the combined Vec — proportional to the full chain height H.

**Existing checks are insufficient:**
- The `start_block_number > last_block_number` check at line 231 only bounds `start_block_number` to the chain tip; it does not cap the Vec sizes.
- There is no post-collection length check before `complete_headers` is called.

## Impact Explanation

With chain height H ≈ 3.4M (CKB mainnet as of 2026) and `start_block_number = H/2 ≈ 1.7M`:
- `reorg_last_n_numbers`: ~1.7M `u64` entries
- `last_n_numbers`: ~1.7M `u64` entries
- `complete_headers` performs ~3.4M DB lookups, block fetches, and MMR root computations

This exhausts process memory and/or CPU, crashing the full-node process. This matches the allowed bounty impact: **High — Vulnerabilities which could easily crash a CKB node** (10001–15000 points).

## Likelihood Explanation

- Any peer connecting via the light-client P2P protocol can send `GetLastStateProof` with no authentication (see `lib.rs` lines 108–112, which dispatch the message directly to `execute()`)
- The only required attacker input is a valid tip hash (publicly observable from the chain) and a `start_hash` that differs from the real ancestor (any random 32-byte value works)
- The overflow is deterministic in release builds, which is the standard CKB deployment configuration
- A single message is sufficient; no repetition or sustained attack is needed

## Recommendation

1. **Fix the overflow** — replace the unchecked multiplication with a saturating operation:
   ```rust
   if self.message.difficulties().len()
       + (last_n_blocks as usize).saturating_mul(2)
       > constant::GET_LAST_STATE_PROOF_LIMIT
   ```

2. **Cap `reorg_last_n_numbers` independently** — after computing `min_block_number`, clamp the range before collecting:
   ```rust
   let capped_start = start_block_number.saturating_sub(
       min(start_block_number, last_n_blocks)
           .min(constant::GET_LAST_STATE_PROOF_LIMIT as u64)
   );
   (capped_start..start_block_number).collect()
   ```

3. **Cap `last_n_numbers` independently** — bound the `(start_block_number..last_block_number)` range by the limit constant.

4. **Add a combined post-collection length check** — assert `reorg_last_n_numbers.len() + last_n_numbers.len() <= GET_LAST_STATE_PROOF_LIMIT` before calling `complete_headers`.

## Proof of Concept

```rust
// Craft the malicious GetLastStateProof message:
let msg = GetLastStateProof {
    last_hash:           /* valid current tip hash, publicly observable */,
    last_n_blocks:       1u64 << 63,       // 2^63 — overflows limit check to 0
    start_number:        1_700_000u64,     // H/2, any value ≤ chain height
    start_hash:          [0u8; 32].into(), // wrong hash → triggers reorg branch
    difficulty_boundary: U256::MAX,
    difficulties:        vec![],           // empty → difficulties.len() = 0
};
// Guard: 0 + (2^63_usize * 2) = 0 > 1000 → false → passes
// reorg_last_n_numbers = (0..1_700_000).collect() → 1.7M entries
// last_n_numbers = (1_700_000..3_400_000).collect() → 1.7M entries
// complete_headers called for ~3.4M entries → OOM / node crash
```

Send this message as any unauthenticated light-client peer. On a chain of height ~3.4M, the node will attempt to allocate and process ~3.4M `packed::VerifiableHeader` objects and their associated MMR roots, exhausting available memory.