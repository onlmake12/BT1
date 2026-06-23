### Title
Integer Overflow in `GetLastStateProof` Size Guard Bypasses Rate Limit, Enabling Unbounded Memory Allocation and DB Reads — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

The only size guard protecting `GetLastStateProofProcess::execute` against oversized requests contains an integer overflow. An unprivileged remote peer can send a crafted `last_n_blocks` value that causes `(last_n_blocks as usize) * 2` to wrap to a small number in Rust release mode, defeating the check and allowing the server to collect and process the entire chain history.

---

### Finding Description

The guard at line 201–205 is:

```rust
let last_n_blocks: u64 = self.message.last_n_blocks().into();

if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // 1000
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
``` [1](#0-0) 

On a 64-bit host, `usize` is 64 bits, so `last_n_blocks as usize` is a lossless cast. However, the subsequent `* 2` multiplication is unchecked. In Rust release builds, integer overflow wraps silently (two's complement). If an attacker sends `last_n_blocks = 0x8000_0000_0000_0001`:

```
(0x8000_0000_0000_0001usize) * 2 = 0x0000_0000_0000_0002  (wraps)
```

`difficulties.len() + 2 > 1000` is `false`, so the guard is bypassed entirely.

After the guard, the code reaches:

```rust
if last_block_number - start_block_number <= last_n_blocks {
    let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
``` [2](#0-1) 

With `last_n_blocks = 0x8000_0000_0000_0001`, the condition `last_block_number - start_block_number <= last_n_blocks` is always true for any realistic chain. The attacker sets `start_block_number = 0` (valid, since only `start_block_number > last_block_number` is rejected), causing the server to collect `(0..last_block_number)` — the entire chain — into a `Vec<BlockNumber>`.

This Vec is then passed to `complete_headers`, which performs three DB reads per entry (`get_ancestor`, `get_block`, `chain_root_mmr`): [3](#0-2) 

The `GET_LAST_STATE_PROOF_LIMIT` constant is 1000: [4](#0-3) 

---

### Impact Explanation

On mainnet with ~10 million blocks, a single crafted request forces:
- Allocation of a `Vec<u64>` with ~10M entries (~80 MB)
- ~30M DB reads (3 per block)
- MMR root computation for each block

Multiple concurrent requests from different peers exhaust memory and saturate I/O, causing a full node DoS. The attacker needs only a valid main-chain block hash (publicly observable) and a valid `start_hash`/`start_number` pair.

---

### Likelihood Explanation

- No authentication or stake required to connect to the light client P2P protocol.
- The crafted field (`last_n_blocks`) is a standard `Uint64` in the molecule schema — any peer can set it to any value.
- Rust release-mode wrapping is deterministic and well-known; the overflow is 100% reproducible.
- The only prerequisite is knowing a valid tip block hash, which is trivially obtained.

---

### Recommendation

Replace the unchecked multiplication with a saturating or checked variant:

```rust
// Option A: saturating_mul — safe, never wraps
if self.message.difficulties().len()
    + (last_n_blocks as usize).saturating_mul(2)
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}

// Option B: reject immediately if last_n_blocks alone exceeds the limit
if last_n_blocks as usize > constant::GET_LAST_STATE_PROOF_LIMIT / 2 {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Additionally, add an independent cap on `last_n_blocks` before any downstream range construction.

---

### Proof of Concept

```rust
// Attacker constructs:
//   last_n_blocks  = 0x8000_0000_0000_0001u64
//   difficulties   = []   (empty)
//   start_number   = 0
//   last_hash      = <any valid mainnet tip hash>
//   start_hash     = <genesis hash>
//   difficulty_boundary = U256::MAX

// Guard evaluation (release mode, 64-bit):
let last_n_blocks: usize = 0x8000_0000_0000_0001usize;
let guard_value = 0usize + last_n_blocks.wrapping_mul(2); // = 2
assert!(guard_value <= 1000); // guard passes — BYPASSED

// Downstream: last_block_number = 10_000_000 (mainnet)
// Condition: 10_000_000 <= 0x8000_0000_0000_0001  → true
// Allocation: (0..10_000_000).collect::<Vec<u64>>()  → 80 MB
// Then: complete_headers iterates 10M entries, 3 DB reads each
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
