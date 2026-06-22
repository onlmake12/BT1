### Title
Integer Overflow in `GET_LAST_STATE_PROOF_LIMIT` Guard Enables DoS via Chain-Height-Proportional Resource Exhaustion — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

The guard at line 201 of `GetLastStateProofProcess::execute` computes `(last_n_blocks as usize) * 2` without overflow protection. An attacker who sends `last_n_blocks = usize::MAX/2 + 1` causes this multiplication to wrap to `0` in Rust release mode, making the entire expression evaluate to `0 > 1000 = false`, silently bypassing the `GET_LAST_STATE_PROOF_LIMIT` check. Execution then proceeds to collect every block number from `start_block_number` to `last_block_number` and perform a full per-block DB lookup chain, with work proportional to the chain height.

---

### Finding Description

**Root cause — integer overflow at the limit guard:**

`last_n_blocks` is decoded directly from the attacker-controlled `Uint64` wire field:

```
last_n_blocks: Uint64,   // schema: extensions.mol line 336
``` [1](#0-0) 

```rust
let last_n_blocks: u64 = self.message.last_n_blocks().into();
``` [2](#0-1) 

The guard then does:

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // 1000
``` [3](#0-2) 

On a 64-bit target, `usize` is 64 bits. The cast `last_n_blocks as usize` is lossless. The multiplication `* 2` is **unchecked** — in Rust release mode, overflow wraps (two's complement). With `last_n_blocks = usize::MAX/2 + 1 = 9223372036854775808`:

```
(9223372036854775808_usize) * 2  →  wraps to 0
0 + 0 = 0  >  1000  →  false   ← guard silently passes
```

**Post-bypass execution path:**

After the guard passes, the condition at line 291 compares `last_block_number - start_block_number <= last_n_blocks` (both `u64`). With `last_n_blocks = 9223372036854775808`, this is true for any realistic chain height, so the code takes the "not enough blocks" branch:

```rust
let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
``` [4](#0-3) 

This allocates a `Vec<u64>` of `(last_block_number - start_block_number)` entries — every block on the chain. Then `complete_headers` iterates over every entry, performing three DB lookups per block (`get_ancestor`, `get_block`, `chain_root_mmr`): [5](#0-4) 

The constant that was supposed to bound this work: [6](#0-5) 

---

### Impact Explanation

- **Memory:** A chain at height N allocates a `Vec<u64>` of N entries (8N bytes), then a `Vec<packed::VerifiableHeader>` of N entries (each header is ~200+ bytes). At 10 million blocks this is ~2 GB of header data alone.
- **CPU/IO:** `complete_headers` performs 3 synchronous DB reads per block. At 10 million blocks this is 30 million DB reads in a single request handler, blocking the async executor.
- **Result:** OOM kill or indefinite handler stall, crashing or freezing the node for all peers.

---

### Likelihood Explanation

- Any unauthenticated peer on the LightClient protocol can send this message.
- The message is a single valid `GetLastStateProof` flatbuffer with `last_n_blocks` set to `9223372036854775808` and `difficulties = []`.
- No PoW, no key, no privileged role required.
- The LightClient protocol must be explicitly enabled, which reduces exposure but does not eliminate it for nodes that run it.

---

### Recommendation

Replace the unchecked arithmetic with a saturating or checked operation:

```rust
// Option A: checked_mul, treat overflow as exceeding the limit
let n_blocks_x2 = (last_n_blocks as usize).checked_mul(2)
    .unwrap_or(usize::MAX);
if self.message.difficulties().len() + n_blocks_x2 > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}

// Option B: reject before the cast if the value already exceeds the limit
if last_n_blocks > (constant::GET_LAST_STATE_PROOF_LIMIT / 2) as u64 {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Either approach closes the bypass before any chain state is accessed.

---

### Proof of Concept

```rust
// Attacker sends:
let content = packed::GetLastStateProof::new_builder()
    .last_hash(valid_tip_hash)
    .start_hash(genesis_hash)
    .start_number(0u64)
    .last_n_blocks(9223372036854775808u64)  // usize::MAX/2 + 1
    .difficulty_boundary(U256::MAX)
    .difficulties(packed::Uint256Vec::default())  // empty
    .build();
```

**Arithmetic trace (release mode, 64-bit):**
```
difficulties.len()          = 0
last_n_blocks as usize      = 9223372036854775808
* 2 (wraps)                 = 0
0 + 0 = 0 > 1000            → false  ← guard bypassed

last_block_number (e.g.)    = 10_000_000
10_000_000 <= 9223372036854775808  → true
→ collect(0..10_000_000)    = Vec of 10M u64s (~80 MB)
→ complete_headers × 10M   = 30M DB reads + ~2 GB VerifiableHeader alloc
```

### Citations

**File:** util/gen-types/schemas/extensions.mol (L336-336)
```text
    last_n_blocks:              Uint64,
```

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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L199-199)
```rust
        let last_n_blocks: u64 = self.message.last_n_blocks().into();
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L201-204)
```rust
        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L291-296)
```rust
        let (sampled_numbers, last_n_numbers) = if last_block_number - start_block_number
            <= last_n_blocks
        {
            // There is not enough blocks, so we take all of them; so there is no sampled blocks.
            let sampled_numbers = Vec::new();
            let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
