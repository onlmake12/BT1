Now I have the full code. Let me analyze the exact arithmetic and control flow.

### Title
Integer Overflow in `GetLastStateProofProcess::execute` Bypasses `GET_LAST_STATE_PROOF_LIMIT`, Enabling Chain-Wide Memory Exhaustion — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

The limit guard at line 201 computes `difficulties.len() + (last_n_blocks as usize) * 2`. In release mode, Rust uses wrapping arithmetic for integer overflow. An attacker who sends `last_n_blocks = u64::MAX/2 + 1` with an empty `difficulties` list causes `(last_n_blocks as usize) * 2` to wrap to `0`, making the guard evaluate `0 > 1000 = false`. The check is silently bypassed. Downstream, the server unconditionally collects every block number from `start_block_number` to the chain tip into a `Vec`, then calls `complete_headers` for each entry — performing multiple DB lookups per block — proportional to the full chain length.

---

### Finding Description

**Overflow site** — `GetLastStateProofProcess::execute`, line 201:

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // 1000
``` [1](#0-0) 

`last_n_blocks` is a raw `u64` decoded directly from the peer message with no prior bound check. [2](#0-1) 

On a 64-bit host `usize` is 64 bits. With `last_n_blocks = 9223372036854775808` (`u64::MAX/2 + 1`):

```
(9223372036854775808_usize) * 2  →  wraps to 0  (release mode)
0 + 0 = 0  >  1000  →  false   ← guard never fires
```

**Unbounded allocation site** — lines 291–296:

```rust
let (sampled_numbers, last_n_numbers) =
    if last_block_number - start_block_number <= last_n_blocks {
        let last_n_numbers =
            (start_block_number..last_block_number).collect::<Vec<_>>();
``` [3](#0-2) 

Because `last_n_blocks` is astronomically large, the condition is always true for any real chain. The server collects every block number from `start_block_number` (attacker sets `0`) to the chain tip into a `Vec<BlockNumber>`.

**Per-entry DB work** — `complete_headers` is then called for every entry in that Vec:

```rust
for number in numbers {
    if let Some(ancestor_header) = self.snapshot.get_ancestor(last_hash, *number) {
        ...
        let ancestor_block = self.snapshot.get_block(&ancestor_header.hash())...;
        let mmr = self.snapshot.chain_root_mmr(*number - 1);
``` [4](#0-3) 

Each iteration performs at minimum three DB operations (`get_ancestor` O(log n), `get_block`, `chain_root_mmr`).

The constant being bypassed: [5](#0-4) 

---

### Impact Explanation

For a CKB mainnet node at ~12 million blocks:

| Resource | Estimate |
|---|---|
| `Vec<BlockNumber>` allocation | 12M × 8 B ≈ **96 MB** |
| `Vec<VerifiableHeader>` (header + uncles_hash + extension + MMR root) | several **GB** |
| DB reads | 12M × 3+ = **36M+ RocksDB lookups** |

A single malicious peer message can trigger an OOM kill or multi-minute CPU stall on a production full node running the light-client server. The attack is repeatable with no rate limit at the message-processing layer.

---

### Likelihood Explanation

- The light-client protocol server is a production feature enabled on nodes that serve light clients.
- The attacker needs only a TCP connection to the node's P2P port — no key, no PoW, no privileged role.
- The crafted message is a single valid protobuf/molecule-encoded `GetLastStateProof` with `last_n_blocks = 9223372036854775808` and `difficulties = []`.
- Release mode (wrapping overflow) is the standard deployment configuration.

---

### Recommendation

Replace the unchecked arithmetic with a saturating or explicit overflow check before the comparison:

```rust
// Option A: saturating arithmetic — overflow saturates to usize::MAX, always > 1000
let total = self.message.difficulties().len()
    .saturating_add((last_n_blocks as usize).saturating_mul(2));
if total > constant::GET_LAST_STATE_PROOF_LIMIT { ... }

// Option B: reject immediately if last_n_blocks itself exceeds the limit
if last_n_blocks as usize > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Either fix closes the bypass before any snapshot or Vec allocation occurs.

---

### Proof of Concept

```rust
// Pseudocode unit test (no chain needed)
let last_n_blocks: u64 = (usize::MAX / 2 + 1) as u64;
let difficulties_len: usize = 0;

// Simulate release-mode wrapping arithmetic
let total = difficulties_len.wrapping_add((last_n_blocks as usize).wrapping_mul(2));
assert_eq!(total, 0);                          // overflow → 0
assert!(total <= 1000);                        // guard is bypassed
// → server proceeds to (0..chain_tip).collect() with no bound
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

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
