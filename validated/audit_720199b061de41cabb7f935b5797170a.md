### Title
Silent Truncation of `max_uncles_num` to `u8` Produces Wrong `uncles_count_limit` in Block Template - (File: `tx-pool/src/block_assembler/mod.rs`)

### Summary
In `BlockTemplateBuilder::new()`, `consensus.max_uncles_num()` (which returns `usize`) is cast to `u8` without any bounds check. If `max_uncles_num` ever exceeds 255, the cast silently wraps/truncates, causing the `uncles_count_limit` field in the block template returned to miners to be wrong — potentially zero or a much smaller value than the actual consensus limit.

### Finding Description
In `tx-pool/src/block_assembler/mod.rs` at line 835:

```rust
let uncles_count_limit = consensus.max_uncles_num() as u8;
```

`max_uncles_num()` returns `usize`. The result is stored in the `BlockTemplate` and `BlockTemplateBuilder` structs, both of which declare `uncles_count_limit: u8`. [1](#0-0) 

The field is typed `u8` in both internal structs: [2](#0-1) [3](#0-2) 

It is then widened back to `u64` for the JSON-RPC response:
```rust
uncles_count_limit: u64::from(template.uncles_count_limit).into(),
``` [4](#0-3) 

The consensus default is `MAX_UNCLE_NUM: usize = 2`, so on mainnet this is safe. [5](#0-4) 

However, the actual consensus enforcement in the uncle verifier uses a separate, independent cast to `u32`:
```rust
let max_uncles_num = self.provider.consensus().max_uncles_num() as u32;
``` [6](#0-5) 

This means the block template's `uncles_count_limit` and the actual consensus enforcement are computed independently. If `max_uncles_num` were configured to 256, the block template would report `uncles_count_limit = 0` (256 wraps to 0 in `u8`), while the verifier would correctly allow 256 uncles.

### Impact Explanation
A miner calling `get_block_template` via RPC would receive an incorrect (truncated) `uncles_count_limit`. If the truncated value is 0, miners would believe they must include zero uncles, causing them to produce blocks with no uncles even when the consensus allows many. This silently reduces miner uncle rewards and misrepresents the consensus rules to all mining software consuming the RPC. The `uncles_count_limit` field is explicitly documented as the authoritative limit miners must respect. [7](#0-6) 

### Likelihood Explanation
**Low.** On mainnet and testnet, `max_uncles_num` is hardcoded to 2, which fits safely in `u8`. The overflow only manifests if a chain is configured with `max_uncles_num > 255`. However, the cast is structurally unsafe and the comment-free code gives no indication that a bounds check is intentionally omitted.

### Recommendation
Replace the bare `as u8` cast with an explicit checked conversion that panics or returns an error if the value exceeds `u8::MAX`:

```rust
let uncles_count_limit = u8::try_from(consensus.max_uncles_num())
    .expect("max_uncles_num must fit in u8");
```

Alternatively, widen `uncles_count_limit` in `BlockTemplate` and `BlockTemplateBuilder` from `u8` to `usize` or `u64` to match the type returned by `max_uncles_num()`, eliminating the narrowing cast entirely.

### Proof of Concept
1. Configure a CKB chain with `max_uncles_num = 256` in the consensus parameters.
2. Start the node and call `get_block_template` via RPC.
3. Observe that the returned `uncles_count_limit` is `0` (256 truncated to `u8` = 0) instead of `256`.
4. Mining software respecting this field will include zero uncles in all produced blocks, forfeiting uncle rewards, while the consensus verifier (using `as u32`) would have accepted up to 256 uncles.

The root cause is the unguarded narrowing cast at: [1](#0-0)

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

**File:** tx-pool/src/block_assembler/mod.rs (L835-835)
```rust
        let uncles_count_limit = consensus.max_uncles_num() as u8;
```

**File:** spec/src/consensus.rs (L46-46)
```rust
const MAX_UNCLE_NUM: usize = 2;
```

**File:** verification/contextual/src/uncles_verifier.rs (L61-61)
```rust
        let max_uncles_num = self.provider.consensus().max_uncles_num() as u32;
```

**File:** util/jsonrpc-types/src/block_template.rs (L57-61)
```rust
    /// The uncle count limit.
    ///
    /// Miners must keep the uncles count below this limit, otherwise, the CKB node will reject the
    /// block submission.
    pub uncles_count_limit: Uint64,
```
