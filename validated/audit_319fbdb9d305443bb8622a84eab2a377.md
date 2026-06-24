Audit Report

## Title
Integer Overflow in `GetLastStateProofProcess::execute` Bypasses Limit Guard, Enabling Full-Chain Traversal DoS — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
`last_n_blocks` is decoded from a P2P message as a `u64` with no prior bounds check. The sole size guard computes `(last_n_blocks as usize) * 2` using unchecked multiplication, which wraps to `0` in Rust release mode when `last_n_blocks = 2^63`. This makes the guard trivially false, allowing the server to collect every block from `start_block_number` to `last_block_number` and pass them all to `complete_headers`, which performs multiple expensive DB reads and MMR root computations per block — O(chain_height) work triggered by a single P2P message.

## Finding Description

**Root cause — unchecked multiplication in the only size guard:**

At line 199, `last_n_blocks` is decoded directly from the attacker-controlled message:
```rust
let last_n_blocks: u64 = self.message.last_n_blocks().into();
``` [1](#0-0) 

The guard at lines 201–205 uses plain `*` multiplication:
```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
``` [2](#0-1) 

In Rust release mode, integer arithmetic wraps. With `last_n_blocks = 0x8000_0000_0000_0000_u64` on a 64-bit target, `last_n_blocks as usize = 0x8000_0000_0000_0000_usize`, and `0x8000_0000_0000_0000_usize * 2 = 0` (two's-complement wrap). With an empty `difficulties` list, the guard evaluates to `0 + 0 > 1000` → `false`. The function proceeds past the only size check.

**Full-chain collection:**

At lines 291–297, the condition `last_block_number - start_block_number <= last_n_blocks` is evaluated with the original (unwrapped) `u64` value `0x8000_0000_0000_0000`. For any realistic chain height (e.g., 10 million blocks), this is trivially true, so the server collects:
```rust
let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
``` [3](#0-2) 

With `start_block_number = 0`, this is the entire chain.

**Expensive per-block work in `complete_headers`:**

For every collected block number, `complete_headers` calls `snapshot.get_ancestor()`, `snapshot.get_block()`, `calc_uncles_hash()`, and `mmr.get_root()`: [4](#0-3) 

On a chain with millions of blocks, this is millions of synchronous DB reads and MMR computations per message.

**The bypassed constant:** [5](#0-4) 

There is no secondary cap on `block_numbers.len()` after assembly at lines 350–354. [6](#0-5) 

**Why all other guards are insufficient:**
- `is_main_chain` (line 210): requires only a valid tip hash, which is publicly observable.
- `start_block_number > last_block_number` (line 231): `start_block_number = 0` always passes.
- `reorg_last_n_numbers` (line 237): with `start_block_number = 0`, this is always empty.
- Difficulty validation (lines 254–288): all checks short-circuit or are vacuously true with an empty `difficulties` list.

## Impact Explanation

A single malicious peer can force the light-client server to allocate a `Vec` of millions of block numbers and then perform O(chain_height) synchronous DB reads and MMR root computations. This exhausts CPU and memory, hanging or crashing the light-client server process. This matches **High: Vulnerabilities which could easily crash a CKB node** (10001–15000 points).

## Likelihood Explanation

The exploit requires only: (1) a valid main chain tip hash (publicly observable via any synced node or block explorer), (2) `start_number = 0`, (3) `difficulties = []`, and (4) `last_n_blocks = 0x8000_0000_0000_0000`. No PoW, stake, or privileged role is required. The message is trivially constructable by any peer. The attack is repeatable with no cooldown.

## Recommendation

Replace the unchecked multiplication with `saturating_mul` before the comparison:

```rust
if self.message.difficulties().len()
    + (last_n_blocks as usize).saturating_mul(2)
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Alternatively, reject `last_n_blocks` values exceeding the limit before any arithmetic:

```rust
if last_n_blocks as usize > constant::GET_LAST_STATE_PROOF_LIMIT / 2 {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

As defense-in-depth, add a hard cap on `block_numbers.len()` after assembly at line 354.

## Proof of Concept

```rust
// In a release build (overflow wraps, no panic):
let last_n_blocks: u64 = 0x8000_0000_0000_0000;
let difficulties_len: usize = 0; // empty difficulties list

// Guard computation wraps to 0:
let check = difficulties_len + (last_n_blocks as usize) * 2; // = 0
assert!(check <= 1000); // passes — guard bypassed

// Server then evaluates (with last_block_number = 10_000_000, start = 0):
let condition = 10_000_000_u64 - 0 <= last_n_blocks; // true
// last_n_numbers = (0..10_000_000).collect() — entire chain
// complete_headers called with 10M block numbers → 10M DB reads + MMR computations
```

Fuzz test: in a release build, send `GetLastStateProof` messages with `last_n_blocks` in `[usize::MAX/2, usize::MAX]`, empty `difficulties`, `start_number = 0`, and a valid tip hash. Assert that `block_numbers.len()` never exceeds `GET_LAST_STATE_PROOF_LIMIT` — the assertion will fire immediately.

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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L350-354)
```rust
        let block_numbers = reorg_last_n_numbers
            .into_iter()
            .chain(sampled_numbers)
            .chain(last_n_numbers)
            .collect::<Vec<_>>();
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
