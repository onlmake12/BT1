### Title
Integer Overflow in `GetLastStateProofProcess::execute` Limit Check Bypasses `GET_LAST_STATE_PROOF_LIMIT`, Enabling O(chain_length) DoS — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

The guard at line 201 uses an unchecked `* 2` multiplication on a `usize` cast of the attacker-controlled `last_n_blocks` field. In Rust release builds, integer overflow wraps silently. An attacker can set `last_n_blocks = 2^63` so the product wraps to `0`, making the check always pass. The server then allocates a `Vec` of size `(last_block_number − start_block_number)` and performs that many expensive DB lookups and MMR root computations — one per block in the chain — per request.

---

### Finding Description

**Overflow in the limit check:** [1](#0-0) 

```
last_n_blocks: u64 = 9223372036854775808  (= 2^63)
(last_n_blocks as usize) * 2              = 0  (wraps on 64-bit in release mode)
0 + difficulties.len() > 1000            = false  → guard not triggered
```

The constant being guarded against: [2](#0-1) 

**Unbounded allocation after bypass:**

With `start_block_number = 0` and `last_n_blocks = 2^63`, the condition `last_block_number − start_block_number <= last_n_blocks` is true for any realistic chain, so the server takes the "not enough blocks" branch: [3](#0-2) 

This collects every block number from `0` to `last_block_number` into a `Vec<u64>`. For a 1 M-block chain that is 8 MB per request. The vector is then passed to `complete_headers`, which performs one `get_ancestor` call, one `get_block` DB lookup, and one `chain_root_mmr(...).get_root()` MMR computation per element: [4](#0-3) 

**Preconditions that are trivially satisfied:**
- `last_hash` must be a valid main-chain block hash — this is public information (any tip hash from a block explorer or peer).
- `start_number = 0`, `difficulties = []`, `difficulty_boundary = 1`.
- The attacker only needs to be a connected peer; no authentication is required.

---

### Impact Explanation

Each crafted message forces the server to allocate O(chain_length) memory and perform O(chain_length) synchronous DB + MMR operations. A small number of concurrent connections sending this message can exhaust RAM and CPU, crashing the node or making it unresponsive. This is a remote, unauthenticated DoS against any light-client-protocol-enabled CKB full node synced to a long chain.

---

### Likelihood Explanation

The attack requires only a TCP connection to the P2P port and knowledge of any valid tip hash (publicly available). No PoW, no key material, no privileged access. The overflow is deterministic and reproducible in any release build on a 64-bit host.

---

### Recommendation

Replace the unchecked multiplication with a saturating or checked variant so that large values of `last_n_blocks` cannot wrap to zero:

```rust
// Before (vulnerable):
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT

// After (safe):
if self.message.difficulties().len()
    + (last_n_blocks as usize).saturating_mul(2)
    > constant::GET_LAST_STATE_PROOF_LIMIT
```

Alternatively, reject the message immediately if `last_n_blocks > GET_LAST_STATE_PROOF_LIMIT / 2` before any cast.

---

### Proof of Concept

```rust
// Pseudocode unit test
let last_n_blocks: u64 = 1u64 << 63; // 9223372036854775808
// Overflow check:
assert_eq!((last_n_blocks as usize).wrapping_mul(2), 0);
// Guard evaluates to: 0 + 0 > 1000 == false  → bypassed

// With a mock chain of N = 1_000_000 blocks:
// last_n_numbers = (0..1_000_000).collect() → 8 MB allocation
// complete_headers called with 1_000_000 entries → 1M DB lookups
```

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
