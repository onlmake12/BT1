I need to carefully trace the arithmetic overflow in the limit check and the subsequent unbounded allocation.

### Title
Wrapping Overflow in `GET_LAST_STATE_PROOF_LIMIT` Guard Enables Unbounded `reorg_last_n_numbers` Allocation — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

In `GetLastStateProofProcess::execute`, the guard that enforces `GET_LAST_STATE_PROOF_LIMIT` uses an unchecked multiplication `(last_n_blocks as usize) * 2`. In Rust release builds, this wraps to zero when `last_n_blocks = 2^63`, silently bypassing the check. An attacker who then triggers the reorg path (by supplying a `start_hash` that does not match the canonical ancestor at `start_block_number`) causes `(0..start_block_number).collect::<Vec<_>>()` to allocate a Vec proportional to the chain height — potentially tens of millions of elements — with no subsequent size bound.

---

### Finding Description

**Overflow in the limit guard** [1](#0-0) 

`last_n_blocks` is a `u64` field from the wire message, cast to `usize`, then multiplied by `2`. On a 64-bit host, `usize` is also 64 bits. Setting `last_n_blocks = 2^63` (i.e., `u64::MAX/2 + 1`) makes `(2^63 as usize) * 2 = 2^64 ≡ 0` under Rust's release-mode wrapping semantics. With an empty `difficulties` list the guard evaluates to `0 + 0 > 1000 = false` and execution continues.

**Unbounded reorg allocation** [2](#0-1) 

When `start_hash` does not match the canonical ancestor at `start_block_number`, the else-branch executes:

```
min_block_number = start_block_number - min(start_block_number, last_n_blocks)
```

Because `last_n_blocks = 2^63 >> start_block_number`, `min(...)` returns `start_block_number`, so `min_block_number = 0`. The range `(0..start_block_number).collect()` then allocates a `Vec<u64>` with exactly `start_block_number` elements — up to the full chain height — before any further validation.

**No subsequent size check** [3](#0-2) 

`reorg_last_n_numbers` is chained directly into `block_numbers` and passed to `complete_headers`, which performs one DB lookup per element. There is no size check on the combined Vec at any point after the bypassed guard.

**The constant being bypassed** [4](#0-3) 

---

### Impact Explanation

Each crafted request with `last_n_blocks = 2^63` and `start_block_number = N` causes:

1. **Memory**: allocation of `N * 8` bytes for the `Vec<u64>` (e.g., ~96 MB for a 12 M-block chain).
2. **CPU/IO**: `complete_headers` then issues `N` sequential DB lookups, each involving `get_ancestor` and `chain_root_mmr`.

A small number of concurrent crafted requests is sufficient to exhaust server memory or saturate I/O, causing OOM termination or extreme latency on the light-client protocol handler. The light-client server runs in the same process as the full node, so a crash or stall affects the entire node.

---

### Likelihood Explanation

The attacker only needs to:
- Know any valid tip hash currently on the main chain (publicly observable).
- Set `start_hash` to any 32-byte value that is not the canonical ancestor at `start_block_number` (trivially satisfied by a random value).
- Set `last_n_blocks = 9223372036854775808` (`0x8000000000000000`).
- Set `difficulties` to an empty list.

No PoW, no key material, no privileged role. Any peer that can open a light-client P2P connection can send this message.

---

### Recommendation

Replace the unchecked multiplication with a saturating or checked variant, and add an explicit cap on `last_n_blocks` before the reorg allocation:

```rust
// Guard: use saturating_mul to prevent wrapping
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

This ensures the reorg Vec is bounded by `GET_LAST_STATE_PROOF_LIMIT` regardless of the wire value.

---

### Proof of Concept

```rust
// Attacker constructs:
let crafted = packed::GetLastStateProof::new_builder()
    .last_hash(known_tip_hash)          // valid tip on main chain
    .start_hash(random_wrong_hash)      // does NOT match ancestor at start_number
    .start_number((N - 1u64).pack())    // large block number, e.g. 12_000_000
    .last_n_blocks(0x8000000000000000u64.pack()) // 2^63 → wraps * 2 to 0
    .difficulty_boundary(U256::max_value().pack())
    // difficulties: empty list (len = 0)
    .build();
// Guard: 0 + (2^63 as usize)*2 = 0 + 0 = 0 > 1000 → false → passes
// Reorg path: min(N-1, 2^63) = N-1 → min_block_number = 0
// Allocation: (0..N-1).collect() → Vec of N-1 u64 values ≈ 96 MB for N=12M
// complete_headers: N-1 sequential DB lookups
```

Sending a handful of these concurrently exhausts server memory or saturates the DB I/O path, causing OOM or extreme latency on the node process.

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
