The code matches the citations exactly. Let me verify the key arithmetic claims:

**L199-205** — `last_n_blocks` is `u64`, cast to `usize` (64-bit on 64-bit hosts), then multiplied by `2` with no overflow protection. In Rust release builds, `(2u64.pow(63) as usize) * 2` wraps to `0`. With an empty `difficulties` list, `0 + 0 > 1000` is `false` — the guard is bypassed.

Audit Report

## Title
Wrapping Overflow in `GET_LAST_STATE_PROOF_LIMIT` Guard Enables Unbounded `reorg_last_n_numbers` Allocation — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
In `GetLastStateProofProcess::execute`, the size guard at L201–205 computes `(last_n_blocks as usize) * 2` without overflow protection. In Rust release builds, setting `last_n_blocks = 2^63` causes this multiplication to wrap to zero, silently bypassing the `GET_LAST_STATE_PROOF_LIMIT` check. An attacker who also supplies a `start_hash` that does not match the canonical ancestor then triggers an allocation of a `Vec<u64>` with up to `start_block_number` elements — bounded only by the chain height — followed by one DB lookup per element in `complete_headers`, enabling OOM or I/O exhaustion on the node.

## Finding Description

**Root cause — overflow in the limit guard (L199–205):**

`last_n_blocks` is a `u64` wire field cast to `usize`. On a 64-bit host, `usize` is also 64 bits. The expression `(last_n_blocks as usize) * 2` uses Rust's default `*` operator, which wraps in release builds. With `last_n_blocks = 0x8000000000000000` (2^63), the product is `2^64 mod 2^64 = 0`. With an empty `difficulties` list, the guard evaluates to `0 + 0 > 1000 = false` and execution continues. [1](#0-0) [2](#0-1) 

**Unbounded reorg allocation (L237–247):**

After the bypassed guard, the reorg path computes:
```
min_block_number = start_block_number - min(start_block_number, last_n_blocks)
```
With `last_n_blocks = 2^63 >> start_block_number`, `min(...)` returns `start_block_number`, so `min_block_number = 0`. The range `(0..start_block_number).collect()` allocates a `Vec<u64>` with exactly `start_block_number` elements — up to the full chain height — before any further validation. [3](#0-2) 

**Second large allocation (L291–296):**

The condition `last_block_number - start_block_number <= last_n_blocks` is always true when `last_n_blocks = 2^63` (chain height is far smaller), so `last_n_numbers = (start_block_number..last_block_number).collect()` also allocates up to chain-height elements. [4](#0-3) 

**No subsequent size check (L350–365):**

Both `reorg_last_n_numbers` and `last_n_numbers` are chained into `block_numbers` and passed directly to `complete_headers`, which issues one `get_ancestor` + `chain_root_mmr` DB lookup per element. There is no size check on the combined Vec at any point after the bypassed guard. [5](#0-4) 

**Existing checks that do not prevent the attack:**

- L210: `is_main_chain(&last_block_hash)` — attacker uses a known valid tip hash; passes.
- L231–235: `start_block_number > last_block_number` — attacker uses a valid block number; passes.
- L237–241: The reorg branch is entered only when `start_hash` does not match the canonical ancestor — trivially satisfied by supplying any random 32-byte value.

## Impact Explanation

Each crafted request causes allocation of approximately `start_block_number * 8` bytes for `reorg_last_n_numbers` plus `(last_block_number - start_block_number) * 8` bytes for `last_n_numbers`, totalling roughly `last_block_number * 8` bytes (e.g., ~96 MB for a 12 M-block chain). `complete_headers` then issues up to `last_block_number` sequential DB lookups. A small number of concurrent crafted requests exhausts server memory or saturates I/O. The light-client server runs in the same process as the full node, so OOM termination or extreme stall affects the entire node.

**Matched impact: High — Vulnerabilities which could easily crash a CKB node (10001–15000 points).**

## Likelihood Explanation

The attacker requires no PoW, no key material, and no privileged role. They only need to:
1. Observe any valid tip hash on the main chain (publicly available).
2. Set `last_n_blocks = 0x8000000000000000`.
3. Set `start_hash` to any value that is not the canonical ancestor at `start_block_number` (a random 32-byte value suffices).
4. Set `difficulties` to an empty list.
5. Open a light-client P2P connection and send the crafted `GetLastStateProof` message.

The attack is repeatable, requires no victim interaction, and can be amplified by sending multiple concurrent requests.

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

Additionally, cap `last_n_blocks` before it is used in the reorg range and in the `last_n_numbers` branch:

```rust
let last_n_blocks = last_n_blocks.min(constant::GET_LAST_STATE_PROOF_LIMIT as u64);
```

This ensures both allocations are bounded by `GET_LAST_STATE_PROOF_LIMIT` regardless of the wire value. [1](#0-0) 

## Proof of Concept

```rust
// Attacker constructs:
let crafted = packed::GetLastStateProof::new_builder()
    .last_hash(known_tip_hash)               // valid tip on main chain
    .start_hash(random_wrong_hash)           // does NOT match ancestor at start_number
    .start_number((12_000_000u64 - 1).pack()) // large block number
    .last_n_blocks(0x8000000000000000u64.pack()) // 2^63 → wraps *2 to 0
    .difficulty_boundary(U256::max_value().pack())
    // difficulties: empty list (len = 0)
    .build();
// Guard: 0 + (2^63 as usize)*2 = 0 + 0 = 0 > 1000 → false → passes
// Reorg path: min(11_999_999, 2^63) = 11_999_999 → min_block_number = 0
// Allocation: (0..11_999_999).collect() → ~96 MB Vec<u64>
// last_n_numbers: (11_999_999..12_000_000).collect() → 1 element
// complete_headers: ~12M sequential DB lookups
```

Sending a handful of these concurrently exhausts server memory or saturates the DB I/O path, causing OOM termination or extreme latency on the node process.

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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L244-247)
```rust
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
