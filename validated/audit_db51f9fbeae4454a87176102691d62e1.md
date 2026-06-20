The overflow is real and exploitable. Here is the analysis:

**Key facts from the code:**

1. `last_n_blocks` is decoded as `u64` from the peer message: [1](#0-0) 

2. The guard computes `(last_n_blocks as usize) * 2` in release mode: [2](#0-1) 

3. `GET_LAST_STATE_PROOF_LIMIT` is `1000`: [3](#0-2) 

4. After the guard, the original `last_n_blocks` (u64) is used unmodified in the branch condition: [4](#0-3) 

---

### Title
Integer Overflow in `GetLastStateProofProcess::execute` Bypasses Rate-Limit Guard, Enabling Unbounded Memory Allocation and DB Reads - (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

### Summary
An unprivileged remote peer can send a `GetLastStateProof` message with `last_n_blocks` set to `usize::MAX/2 + 1` (e.g., `0x8000000000000001` on 64-bit). In release mode, `(last_n_blocks as usize) * 2` wraps to `2`, bypassing the `GET_LAST_STATE_PROOF_LIMIT = 1000` guard. The original enormous `last_n_blocks` value is then used in the branch condition, causing the server to collect every block number in the chain into a `Vec` and perform a DB read per block.

### Finding Description

At line 199, `last_n_blocks` is a `u64` from the peer-controlled message:

```rust
let last_n_blocks: u64 = self.message.last_n_blocks().into();
```

The guard at lines 201–205 is:

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT  // 1000
```

On a 64-bit host, `usize` is 64 bits. The cast `last_n_blocks as usize` is a no-op. The subsequent `* 2` is a `usize` multiplication. In Rust **release builds**, integer overflow wraps silently (no panic). With `last_n_blocks = 0x8000000000000001`:

```
(0x8000000000000001_usize) * 2 = 0x0000000000000002  // wraps to 2
```

So `0 + 2 > 1000` is `false` — the guard is bypassed.

After the guard, the original `last_n_blocks = 0x8000000000000001` is used at line 291:

```rust
if last_block_number - start_block_number <= last_n_blocks {
    let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
```

Since `last_n_blocks` is `~9.2 × 10^18`, this condition is trivially true for any real chain. The server collects every block number from `start_block_number` to `last_block_number` into a `Vec`, then calls `complete_headers` which performs multiple DB reads (block hash, block ext, block body, MMR root) per entry. [4](#0-3) [5](#0-4) 

### Impact Explanation
A single malicious peer message causes the server to allocate a `Vec<BlockNumber>` proportional to the entire chain height, then perform O(chain_height) DB reads and MMR root computations. On mainnet (millions of blocks), this is an OOM/DoS condition. The attacker needs no credentials, no PoW, and no stake — only a valid P2P connection to a light-client-protocol-enabled node.

### Likelihood Explanation
The light client protocol server is reachable by any peer. The malformed value is a single field in a flatbuffer-encoded message. No special chain state is required. The overflow only manifests in release builds (which is what production nodes run), making it invisible in debug/test environments.

### Recommendation
Replace the overflow-prone guard with a saturating or checked operation, and validate `last_n_blocks` against the limit **before** casting:

```rust
// Option 1: reject before any arithmetic
if last_n_blocks as usize > constant::GET_LAST_STATE_PROOF_LIMIT / 2 {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}

// Option 2: use saturating_mul
if self.message.difficulties().len()
    + (last_n_blocks as usize).saturating_mul(2)
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Additionally, add a `#[deny(arithmetic_overflow)]` or enable `overflow-checks = true` in the release profile in `Cargo.toml`.

### Proof of Concept

```rust
// Craft a GetLastStateProof message with:
//   last_n_blocks = 0x8000000000000001  (usize::MAX/2 + 1 on 64-bit)
//   difficulties  = []  (empty)
//
// Guard evaluation in release mode:
//   (0x8000000000000001_usize) * 2 = 0x2  (wraps)
//   0 + 2 > 1000  =>  false  =>  guard NOT triggered
//
// Branch evaluation:
//   last_block_number - 0 <= 0x8000000000000001  =>  always true
//   => (0..last_block_number).collect::<Vec<_>>()  allocates entire chain
//   => complete_headers() performs ~N * 4 DB reads for N = chain height
```

Send this message repeatedly from a single peer to exhaust server memory and I/O.

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L124-180)
```rust
    fn complete_headers(
        &self,
        positions: &mut Vec<u64>,
        last_hash: &packed::Byte32,
        numbers: &[BlockNumber],
    ) -> Result<Vec<packed::VerifiableHeader>, String> {
        let mut headers = Vec::new();

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

                headers.push(header);
            } else {
                let errmsg = format!("failed to find ancestor header ({number})");
                return Err(errmsg);
            }
        }

        Ok(headers)
    }
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
