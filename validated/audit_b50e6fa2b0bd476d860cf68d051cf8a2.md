Looking at the actual code, I need to verify each step of the claimed exploit chain.

The exploit chain is concrete and traceable through the production code. Here is the full analysis:

---

### Title
Integer Overflow in `GetLastStateProofProcess::execute` Bypasses `GET_LAST_STATE_PROOF_LIMIT`, Enabling Unbounded Per-Request Work — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

An unauthenticated remote peer can send a `GetLastStateProof` P2P message with `last_n_blocks = 2^63` (a valid `Uint64` value). In release mode, the guard expression `(last_n_blocks as usize) * 2` wraps to `0`, silently bypassing the `GET_LAST_STATE_PROOF_LIMIT = 1000` check. The server then processes up to the entire chain history per request — allocating Vecs and performing per-block DB lookups, MMR root computations, and header builds — with no effective upper bound.

---

### Finding Description

**Root cause — integer overflow at the guard:**

`last_n_blocks` is decoded as `u64` from the wire message: [1](#0-0) 

The schema confirms `last_n_blocks` is a `Uint64` field with no range restriction: [2](#0-1) 

On a 64-bit host, `usize` is also 64 bits. With `last_n_blocks = 2^63`:
- `last_n_blocks as usize = 2^63`
- `(2^63) * 2 = 2^64` → wraps to `0` in release mode (Rust's default wrapping-on-overflow behavior in non-debug builds)
- The guard becomes `difficulties.len() + 0 > 1000`, which is `false` for any message with ≤ 1000 difficulties

The limit constant being bypassed: [3](#0-2) 

**Consequence 1 — entire chain range collected:**

After the guard passes, the branch at line 291 compares `last_block_number - start_block_number <= last_n_blocks`. With `last_n_blocks = 2^63`, this is always true for any realistic chain. The server then collects every block number from `start_block_number` to `last_block_number` into a Vec with no cap: [4](#0-3) 

**Consequence 2 — reorg path also unbounded:**

If the attacker sets `start_block_number` to a non-ancestor hash, the reorg path at line 245 also collects `(0..start_block_number)` because `min(start_block_number, 2^63) = start_block_number`: [5](#0-4) 

**Consequence 3 — per-block work in `complete_headers`:**

For every collected block number, `complete_headers` performs `get_ancestor`, `get_block`, `chain_root_mmr(...).get_root()`, and builds a `VerifiableHeader`. This is significant CPU and I/O per entry: [6](#0-5) 

---

### Impact Explanation

A chain of N blocks causes O(N) allocations and O(N) DB+MMR operations per malicious request. For a mainnet node with millions of blocks, a single crafted request forces the server to process the entire chain history. Multiple concurrent requests compound this into memory exhaustion and CPU saturation, crashing the node. The intended invariant — that `GET_LAST_STATE_PROOF_LIMIT = 1000` bounds per-request work — is completely defeated.

---

### Likelihood Explanation

- No authentication or PoW is required; any peer on the light client P2P port can send this message.
- The attacker only needs a valid main-chain block hash for `last_hash` (trivially obtained from any public explorer or by syncing a single block).
- `start_block_number = 0` maximizes the range and is always valid.
- The overflow value `2^63` is a single fixed constant; no brute force is needed.

---

### Recommendation

Replace the unchecked multiplication with a saturating or checked cast before the comparison:

```rust
// Option A: saturating_mul prevents wrap-to-zero
if self.message.difficulties().len()
    + (last_n_blocks as usize).saturating_mul(2)
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Additionally, add an explicit upper-bound check on `last_n_blocks` itself before any arithmetic:

```rust
if last_n_blocks as usize > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

---

### Proof of Concept

```rust
// In release mode (cargo test --release):
let last_n_blocks: u64 = (usize::MAX / 2 + 1) as u64; // = 2^63
let difficulties_len: usize = 0;

// Simulates the guard expression:
let guard = difficulties_len + (last_n_blocks as usize) * 2;
// guard == 0 (wraps), so 0 > 1000 is false → limit check bypassed

// Simulates the branch:
let chain_len: u64 = 100_000;
let start_block_number: u64 = 0;
let last_block_number: u64 = chain_len;
assert!(last_block_number - start_block_number <= last_n_blocks); // true

// Server collects 100,000 entries instead of ≤ 1,000:
let last_n_numbers: Vec<u64> = (start_block_number..last_block_number).collect();
assert_eq!(last_n_numbers.len(), 100_000); // violates GET_LAST_STATE_PROOF_LIMIT
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L245-246)
```rust
            let min_block_number = start_block_number - min(start_block_number, last_n_blocks);
            (min_block_number..start_block_number).collect()
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

**File:** util/gen-types/schemas/extensions.mol (L336-336)
```text
    last_n_blocks:              Uint64,
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
