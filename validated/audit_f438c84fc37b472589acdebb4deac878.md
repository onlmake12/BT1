Looking at the exact code in question:

### Title
Integer Overflow in `GET_LAST_STATE_PROOF_LIMIT` Guard Allows O(chain_length) Work Per Request — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

The limit guard in `GetLastStateProofProcess::execute` computes `(last_n_blocks as usize) * 2` without overflow protection. In Rust release mode (where `overflow-checks = false` by default), a crafted `last_n_blocks` value causes this multiplication to wrap to a small number, bypassing the `GET_LAST_STATE_PROOF_LIMIT = 1000` check. The same field is then used as its original large `u64` value in a downstream comparison, causing the server to collect and process every block in the chain.

---

### Finding Description

**Stage 1 — Overflow in the guard:** [1](#0-0) 

```rust
let last_n_blocks: u64 = self.message.last_n_blocks().into();

if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // 1000
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

On a 64-bit target `usize` is 64 bits, so `last_n_blocks as usize` is lossless. With `last_n_blocks = 0x8000000000000001u64`:

```
0x8000000000000001usize * 2  =  0x0000000000000002usize  (wraps in release mode)
0 + 2 > 1000  →  false  →  guard passes
```

**Stage 2 — Large value used downstream:** [2](#0-1) 

```rust
let (sampled_numbers, last_n_numbers) = if last_block_number - start_block_number
    <= last_n_blocks          // ← original u64, ≈ 9.2×10^18
{
    let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
    ...
```

Because `last_n_blocks` is still `0x8000000000000001u64` here (no overflow occurred in this comparison), the condition is always true for any realistic chain. The server collects every block number from `start_block_number` to `last_block_number` into a `Vec`.

**Stage 3 — O(chain_length) MMR + DB work:** [3](#0-2) 

`complete_headers` then iterates every collected block number, calling `snapshot.get_ancestor(...)`, `snapshot.get_block(...)`, and `snapshot.chain_root_mmr(*number - 1).get_root()` for each — all expensive DB and MMR operations. [4](#0-3) 

---

### Impact Explanation

A single malicious P2P peer can send a `GetLastStateProof` message with `last_n_blocks = 0x8000000000000001` and `difficulties = []`. The server performs O(chain_length) DB reads and MMR root computations per message. On a chain with millions of blocks this causes severe CPU and memory exhaustion. The attack is repeatable at will with no rate limit beyond the P2P connection layer, and affects every node running the light-client protocol server in release mode. [5](#0-4) 

---

### Likelihood Explanation

- Any unauthenticated peer can send a `GetLastStateProof` message.
- The crafted value fits in a standard `u64` field; no special privileges are needed.
- Release builds of CKB have `overflow-checks = false` by default (standard Rust release profile), making the wrap deterministic.
- The exploit is reproducible with a single message.

---

### Recommendation

Replace the unchecked multiplication with a saturating or checked variant so that any value large enough to overflow is treated as exceeding the limit:

```rust
let total_samples = self.message.difficulties().len()
    .saturating_add((last_n_blocks as usize).saturating_mul(2));
if total_samples > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Alternatively, reject any `last_n_blocks > GET_LAST_STATE_PROOF_LIMIT / 2` before the multiplication. [6](#0-5) 

---

### Proof of Concept

```rust
// Attacker sends GetLastStateProof with:
//   last_n_blocks = 0x8000000000000001u64
//   difficulties  = []
//   last_hash     = <any valid tip hash>
//   start_number  = 0

// Guard evaluation (release mode, 64-bit):
let last_n_blocks: u64 = 0x8000000000000001u64;
let check = 0usize + (last_n_blocks as usize).wrapping_mul(2); // = 2
assert!(check <= 1000); // passes — guard bypassed

// Downstream: last_n_blocks as u64 is still 0x8000000000000001
// last_block_number - 0 <= 0x8000000000000001  →  always true
// → collects (0..last_block_number) → O(chain_length) entries
// → complete_headers does O(chain_length) MMR + DB ops
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L356-366)
```rust
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
        };
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
