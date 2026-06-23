### Title
`uncles_count_limit` Silently Truncated to `u8` in Block Template Assembly — (`File: tx-pool/src/block_assembler/mod.rs`)

### Summary
`BlockTemplateBuilder::new()` casts `consensus.max_uncles_num()` (a `usize`) to `u8` when populating `uncles_count_limit`. This silent truncation means any chain configuration with `max_uncles_num > 255` would cause the `get_block_template` RPC to report a wrong (truncated) uncle count limit to miners, while the actual consensus verifier enforces the correct value. Miners relying on the template's `uncles_count_limit` field would under-include uncles, limiting the protocol's uncle-inclusion functionality.

### Finding Description

`BlockTemplate` and `BlockTemplateBuilder` both declare `uncles_count_limit` as `u8`: [1](#0-0) 

In `BlockTemplateBuilder::new()`, the consensus value is cast without bounds checking: [2](#0-1) 

`consensus.max_uncles_num()` returns `usize`: [3](#0-2) 

The JSON serialization widens the already-truncated `u8` back to `Uint64`: [4](#0-3) 

However, the actual consensus enforcement in `UnclesVerifier` casts to `u32`, not `u8`, so it uses the correct value: [5](#0-4) 

This creates a split: the block template reports a truncated limit to miners, while the verifier enforces the real limit. For any `max_uncles_num` value that does not fit in `u8` (i.e., > 255), the two values diverge. For example, if `max_uncles_num = 256`, the template reports `uncles_count_limit = 0`, causing miners to include zero uncles, while the verifier would accept up to 256.

### Impact Explanation

**Impact: Low.** Miners using `get_block_template` receive an incorrect `uncles_count_limit` in the template response. They would under-include uncles relative to what the protocol actually allows, reducing uncle rewards and orphan-rate compensation. No funds are lost and no invalid blocks are accepted (the verifier uses the correct value). The protocol's uncle-inclusion mechanism is functionally limited for affected configurations.

### Likelihood Explanation

**Likelihood: Low on mainnet** (current `MAX_UNCLE_NUM = 2`), but **High for custom chain specs** or future protocol upgrades that raise the uncle limit above 255. The bug is latent and would activate silently without any error or warning. [3](#0-2) 

### Recommendation

Change the type of `uncles_count_limit` in both `BlockTemplate` and `BlockTemplateBuilder` from `u8` to `usize` (or at minimum `u32`, matching the verifier). Add a checked cast with an explicit error if the value exceeds the representable range:

```rust
// Instead of:
let uncles_count_limit = consensus.max_uncles_num() as u8;

// Use:
let uncles_count_limit = consensus.max_uncles_num();
// and change the field type to usize/u32
``` [6](#0-5) 

### Proof of Concept

1. Configure a chain spec with `max_uncles_num = 300`.
2. Start a CKB node with this spec.
3. Call `get_block_template` via RPC.
4. Observe `uncles_count_limit` in the response equals `44` (`300 % 256`) instead of `300`.
5. A miner following the template would include at most 44 uncles, while the consensus verifier (`uncles_verifier.rs` line 61, casting to `u32`) would accept up to 300.
6. The miner misses valid uncle inclusion opportunities for every block, reducing uncle rewards and degrading the protocol's orphan-rate compensation mechanism. [7](#0-6) [8](#0-7)

### Citations

**File:** tx-pool/src/block_assembler/mod.rs (L757-757)
```rust
    pub(crate) uncles_count_limit: u8,
```

**File:** tx-pool/src/block_assembler/mod.rs (L780-780)
```rust
            uncles_count_limit: u64::from(template.uncles_count_limit).into(),
```

**File:** tx-pool/src/block_assembler/mod.rs (L806-806)
```rust
    pub(crate) uncles_count_limit: u8,
```

**File:** tx-pool/src/block_assembler/mod.rs (L833-836)
```rust
        let max_block_bytes = consensus.max_block_bytes();
        let cycles_limit = consensus.max_block_cycles();
        let uncles_count_limit = consensus.max_uncles_num() as u8;

```

**File:** spec/src/consensus.rs (L46-46)
```rust
const MAX_UNCLE_NUM: usize = 2;
```

**File:** verification/contextual/src/uncles_verifier.rs (L60-68)
```rust
        // verify uncles length =< max_uncles_num
        let max_uncles_num = self.provider.consensus().max_uncles_num() as u32;
        if uncles_count > max_uncles_num {
            return Err(UnclesError::OverCount {
                max: max_uncles_num,
                actual: uncles_count,
            }
            .into());
        }
```
