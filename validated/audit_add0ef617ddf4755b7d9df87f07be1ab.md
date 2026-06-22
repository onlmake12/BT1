Looking at the actual code, I need to verify the overflow claim and trace the full execution path.

The code confirms the vulnerability. There is no `checked_mul`, `saturating_mul`, or any other overflow guard on the limit check expression. Here is the full analysis:

---

### Title
Integer Overflow in `GET_LAST_STATE_PROOF_LIMIT` Check Allows Remote DoS via Unbounded Heap Allocation — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

### Summary
In `GetLastStateProofProcess::execute`, the guard that enforces `GET_LAST_STATE_PROOF_LIMIT` uses plain Rust `*` on `usize`. In release mode, Rust integer arithmetic wraps on overflow. An attacker can supply `last_n_blocks = 2^63` (a valid `u64`), causing `(last_n_blocks as usize) * 2` to wrap to `0`, bypassing the limit check entirely. The server then collects every block number from `start_block_number` to the chain tip into a `Vec`, followed by per-block DB lookups and `VerifiableHeader` construction for each entry — exhausting heap memory and crashing the node.

### Finding Description

**The overflow** occurs at: [1](#0-0) 

```rust
let last_n_blocks: u64 = self.message.last_n_blocks().into();

if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

On a 64-bit host, `usize` is 64 bits. With `last_n_blocks = 2^63`:
- `last_n_blocks as usize = 2^63`
- `(2^63_usize) * 2 = 0` (wraps in release mode — no panic, no saturation)
- `0 + 0 > 1000` → **false** → guard is skipped

`GET_LAST_STATE_PROOF_LIMIT` is `1000`: [2](#0-1) 

There is no `checked_mul`, `saturating_mul`, or `wrapping_mul` anywhere in this file: [3](#0-2) 

**The unbounded allocation** follows at: [4](#0-3) 

```rust
let (sampled_numbers, last_n_numbers) = if last_block_number - start_block_number
    <= last_n_blocks
{
    let sampled_numbers = Vec::new();
    let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
    (sampled_numbers, last_n_numbers)
```

`last_n_blocks` here is the **original u64 value** (`2^63`), not the overflowed `usize`. Any realistic chain height (e.g., 12M) satisfies `12M <= 2^63`, so the branch is taken and `last_n_numbers` collects all ~12M block numbers.

**The per-block work** in `complete_headers` then executes for each of those 12M entries: [5](#0-4) 

For each block number: `get_ancestor`, `get_block`, `calc_uncles_hash`, `chain_root_mmr(...).get_root()`, and a `VerifiableHeader` push. At ~200–400 bytes per `VerifiableHeader`, 12M entries = 2.4–4.8 GB of heap allocation, plus millions of RocksDB reads.

### Impact Explanation
A single malformed P2P message from an unprivileged light-client peer causes the full node to attempt gigabytes of heap allocation and millions of DB lookups. This crashes or hangs the node process, taking it off the network. No authentication, PoW, or privileged access is required.

### Likelihood Explanation
The light-client protocol server is reachable by any peer that connects on the light-client P2P port. The crafted message is trivially constructable: set `last_n_blocks = (1u64 << 63)`, `difficulties = []`, `last_hash` = any valid main-chain tip hash, `start_number = 0`. No prior state or chain knowledge beyond the tip hash is needed.

### Recommendation
Replace the plain multiplication with overflow-safe arithmetic:

```rust
// Before (vulnerable):
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT

// After (safe):
let n = (last_n_blocks as usize)
    .saturating_mul(2)
    .saturating_add(self.message.difficulties().len());
if n > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Alternatively, reject any `last_n_blocks > GET_LAST_STATE_PROOF_LIMIT` before the cast.

### Proof of Concept

```rust
// Pseudocode: craft a GetLastStateProof message
let msg = GetLastStateProof {
    last_hash:          chain_tip_hash,   // valid main-chain tip
    start_hash:         Byte32::zero(),
    start_number:       0u64,
    last_n_blocks:      1u64 << 63,       // 2^63 — overflows (usize)*2 to 0
    difficulty_boundary: U256::max_value(),
    difficulties:       vec![],           // empty → len() = 0
};
// Limit check: 0 + (2^63_usize * 2 = 0) > 1000 → false → passes
// Branch: (chain_height - 0) <= 2^63 → true
// last_n_numbers = (0..chain_height).collect() → ~12M entries
// complete_headers iterates all 12M → OOM / node crash
```

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L1-13)
```rust
use std::{
    cmp::{Ordering, min},
    sync::Arc,
};

use ckb_merkle_mountain_range::leaf_index_to_pos;
use ckb_network::{CKBProtocolContext, PeerIndex};
use ckb_shared::Snapshot;
use ckb_store::ChainStore;
use ckb_types::{U256, core::BlockNumber, packed, prelude::*};

use crate::{LightClientProtocol, Status, StatusCode, constant};

```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L132-170)
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

                let header = packed::VerifiableHeader::new_builder()
                    .header(ancestor_header.data())
                    .uncles_hash(uncles_hash)
                    .extension(Pack::pack(&extension))
                    .parent_chain_root(parent_chain_root)
                    .build();
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
