The actual code matches the cited lines exactly. Let me verify the overflow behavior and exploit path against the real code.

Audit Report

## Title
Wrapping Overflow in `GET_LAST_STATE_PROOF_LIMIT` Guard Enables Unbounded `reorg_last_n_numbers` Allocation — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
In `GetLastStateProofProcess::execute`, the guard enforcing `GET_LAST_STATE_PROOF_LIMIT` computes `(last_n_blocks as usize) * 2` with unchecked multiplication. In Rust release builds, setting `last_n_blocks = 2^63` causes this to wrap to zero, silently bypassing the limit. An attacker who then supplies a `start_hash` that does not match the canonical ancestor at `start_block_number` triggers the reorg path, which allocates a `Vec<u64>` of `start_block_number` elements — proportional to the full chain height — with no subsequent size bound, followed by an equal number of sequential DB lookups in `complete_headers`.

## Finding Description

**Overflow in the limit guard** — [1](#0-0) 

`last_n_blocks` is a `u64` wire field cast to `usize` (also 64 bits on 64-bit hosts), then multiplied by `2` with the plain `*` operator. Rust's release-mode semantics wrap on overflow: `(2^63_usize) * 2 = 2^64 ≡ 0`. With an empty `difficulties` list the guard evaluates to `0 + 0 > 1000 = false` and execution continues unchecked.

**Unbounded reorg allocation** — [2](#0-1) 

When `start_hash` does not match the canonical ancestor at `start_block_number`, the else-branch executes:
```
min_block_number = start_block_number - min(start_block_number, last_n_blocks)
```
With `last_n_blocks = 2^63 >> start_block_number`, `min(...)` returns `start_block_number`, so `min_block_number = 0`. The range `(0..start_block_number).collect()` allocates a `Vec<u64>` with exactly `start_block_number` elements before any further validation.

**No subsequent size check** — [3](#0-2) 

`reorg_last_n_numbers` is chained directly into `block_numbers` and passed to `complete_headers`, which issues one `get_ancestor` + `chain_root_mmr` DB lookup per element. There is no size check on the combined Vec at any point after the bypassed guard. A grep for `saturating`, `checked_mul`, or `wrapping` across the entire `util/light-client-protocol-server/` directory returns zero matches, confirming no existing mitigation.

**The bypassed constant** — [4](#0-3) 

`GET_LAST_STATE_PROOF_LIMIT = 1000` is the sole intended cap; the overflow renders it ineffective.

## Impact Explanation

Each crafted request with `last_n_blocks = 2^63` and `start_block_number = N` causes:
- **Memory**: allocation of `N × 8` bytes for the `Vec<u64>` (≈ 96 MB for a 12 M-block chain).
- **CPU/IO**: `complete_headers` then issues `N` sequential DB lookups, each involving `get_ancestor` and `chain_root_mmr`.

A small number of concurrent crafted requests is sufficient to exhaust server memory or saturate I/O, causing OOM termination or extreme latency. The light-client server runs in the same process as the full node, so a crash or stall affects the entire node.

**Impact class: High — "Vulnerabilities which could easily crash a CKB node" (10001–15000 points).**

## Likelihood Explanation

The attacker requires no PoW, no key material, and no privileged role. They only need:
1. Any valid tip hash currently on the main chain (publicly observable via P2P gossip).
2. Any 32-byte `start_hash` that is not the canonical ancestor at `start_block_number` (trivially satisfied by a random value).
3. `last_n_blocks = 0x8000000000000000` (`2^63`).
4. An empty `difficulties` list.

Any peer that can open a light-client P2P connection can send this message repeatedly. The attack is fully repeatable and requires no victim interaction.

## Recommendation

Replace the unchecked multiplication with `saturating_mul` to prevent wrapping:

```rust
if self.message.difficulties().len()
    + (last_n_blocks as usize).saturating_mul(2)
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Additionally, cap `last_n_blocks` itself before it is used in the reorg range:

```rust
let last_n_blocks = last_n_blocks.min(constant::GET_LAST_STATE_PROOF_LIMIT as u64);
```

This ensures the reorg `Vec` is bounded by `GET_LAST_STATE_PROOF_LIMIT` regardless of the wire value.

## Proof of Concept

```rust
// Attacker constructs:
let crafted = packed::GetLastStateProof::new_builder()
    .last_hash(known_tip_hash)              // valid tip on main chain
    .start_hash(random_wrong_hash)          // does NOT match ancestor at start_number
    .start_number((N - 1u64).pack())        // e.g. 12_000_000
    .last_n_blocks(0x8000000000000000u64.pack()) // 2^63 → wraps *2 to 0
    .difficulty_boundary(U256::max_value().pack())
    // difficulties: empty list (len = 0)
    .build();
// Guard: 0 + (2^63 as usize)*2 = 0 + 0 = 0 > 1000 → false → passes
// Reorg path: min(N-1, 2^63) = N-1 → min_block_number = 0
// Allocation: (0..N-1).collect() → Vec of N-1 u64 values ≈ 96 MB for N=12M
// complete_headers: N-1 sequential DB lookups
```

Sending a handful of these concurrently exhausts server memory or saturates the DB I/O path, causing OOM or extreme latency on the node process. A unit test can confirm the overflow by asserting `(usize::MAX/2 + 1).wrapping_mul(2) == 0` and verifying the guard condition evaluates to `false`.

### Citations

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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L350-365)
```rust
        let block_numbers = reorg_last_n_numbers
            .into_iter()
            .chain(sampled_numbers)
            .chain(last_n_numbers)
            .collect::<Vec<_>>();

        let (positions, headers) = {
            let mut positions: Vec<u64> = Vec::new();
            let headers =
                match sampler.complete_headers(&mut positions, &last_block_hash, &block_numbers) {
                    Ok(headers) => headers,
                    Err(errmsg) => {
                        return StatusCode::InternalError.with_context(errmsg);
                    }
                };
            (positions, headers)
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
