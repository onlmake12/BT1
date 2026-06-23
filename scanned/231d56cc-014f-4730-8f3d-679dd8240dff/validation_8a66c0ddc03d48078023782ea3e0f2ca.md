### Title
Integer Overflow in `GetLastStateProofProcess::execute()` Guard Bypasses Per-Message Work Limit — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

The guard that enforces `GET_LAST_STATE_PROOF_LIMIT` (1000) in `GetLastStateProofProcess::execute()` contains an unchecked multiplication `(last_n_blocks as usize) * 2`. In Rust release builds, integer overflow wraps silently. An attacker who sends `last_n_blocks = usize::MAX/2 + 1` (a valid `u64` value) causes the product to wrap to `0`, making the guard trivially false. Execution then proceeds to collect and serve the entire chain range, performing O(chain_length) database reads per message with no bound.

---

### Finding Description

The guard at lines 201–205 of `get_last_state_proof.rs` is:

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // 1000
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
``` [1](#0-0) 

`last_n_blocks` is a `u64` decoded directly from the peer message with no prior range check:

```rust
let last_n_blocks: u64 = self.message.last_n_blocks().into();
``` [2](#0-1) 

On a 64-bit host, `usize` is 64 bits. Setting `last_n_blocks = 2^63` (i.e., `usize::MAX/2 + 1`, a legal `u64`):

- `last_n_blocks as usize` = `2^63`
- `2^63 * 2` = `2^64` → wraps to **0** in release mode
- `difficulties.len() + 0 > 1000` → **false** → guard is skipped

No `checked_mul`, `saturating_mul`, or `wrapping_*` annotation exists anywhere in the file; the grep for overflow-safe arithmetic returns zero matches. [3](#0-2) 

After the guard is bypassed, the code reaches:

```rust
let (sampled_numbers, last_n_numbers) = if last_block_number - start_block_number
    <= last_n_blocks   // 2^63 — always true for any real chain
{
    let sampled_numbers = Vec::new();
    let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
    ...
``` [4](#0-3) 

With `start_block_number = 0`, `last_n_numbers` spans the **entire chain**. Every entry is then passed to `complete_headers`, which performs `get_ancestor` + `get_block` + `chain_root_mmr` DB reads per block: [5](#0-4) 

The `GET_LAST_STATE_PROOF_LIMIT` constant is 1000: [6](#0-5) 

---

### Impact Explanation

A single crafted `GetLastStateProof` message with `last_n_blocks = 2^63`, `difficulties = []`, `start_number = 0`, and a valid `last_hash` causes the server to iterate and perform multiple DB reads for every block in the chain. On a mainnet node with millions of blocks this is unbounded CPU and I/O work per message. Any unprivileged peer can send this message repeatedly, exhausting the full node's resources and denying service to all light clients it serves.

---

### Likelihood Explanation

The `GetLastStateProof` message is accepted from any peer over the light-client P2P protocol with no authentication or rate-limiting visible in the handler path. The overflow value `2^63` is a single field in a small, otherwise valid message. The exploit requires no hashpower, no key material, and no privileged access.

---

### Recommendation

Replace the unchecked arithmetic in the guard with overflow-safe operations:

```rust
let n = (last_n_blocks as usize)
    .checked_mul(2)
    .unwrap_or(usize::MAX);
if self.message.difficulties().len().saturating_add(n)
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Additionally, add an explicit upper-bound check on `last_n_blocks` itself (e.g., `last_n_blocks > GET_LAST_STATE_PROOF_LIMIT as u64`) before any arithmetic to reject obviously malicious values early.

---

### Proof of Concept

```rust
// Craft a minimal malicious GetLastStateProof message
let malicious = packed::GetLastStateProof::new_builder()
    .last_hash(valid_tip_hash)
    .start_hash(genesis_hash)
    .start_number(0u64.pack())
    // 2^63: overflows (usize::MAX/2 + 1) * 2 → 0 in release mode
    .last_n_blocks((((usize::MAX / 2) + 1) as u64).pack())
    .difficulty_boundary(U256::from(u64::MAX).pack())
    // empty difficulties: 0 + 0 > 1000 is false → guard bypassed
    .difficulties(packed::Uint256Vec::default())
    .build();
```

Send this message to any full node running the light-client protocol server. In release mode the guard evaluates `0 + 0 > 1000 = false`, execution falls through, and the server iterates every block from genesis to tip performing multiple DB reads each, with no bound.

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L132-146)
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

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
