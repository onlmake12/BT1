Let me verify the exact arithmetic and any other guards in the code.

The overflow is real and the exploit path is concrete. Here is the full analysis:

---

### Title
Integer Overflow in `GetLastStateProof` Limit Check Bypasses `GET_LAST_STATE_PROOF_LIMIT` Cap — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

The guard that enforces the 1000-sample cap on `GetLastStateProof` requests performs an unchecked `usize` multiplication in release mode. An unprivileged remote peer can supply `last_n_blocks = 2^63` (a valid `u64`), causing `(last_n_blocks as usize) * 2` to wrap to `0`, making the check trivially false. The server then proceeds to allocate and process a number of block entries proportional to the actual chain height — potentially millions — violating the invariant that all per-request work is bounded by 1000.

---

### Finding Description

**Overflow site** — `execute()`, line 201:

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // 1000
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
``` [1](#0-0) 

`last_n_blocks` is decoded as a plain `u64` with no prior range check: [2](#0-1) 

On a 64-bit target `usize` is 64 bits. Rust's release build uses **wrapping** (two's-complement) semantics for integer arithmetic — there is no panic, no saturation, no UB. Therefore:

```
last_n_blocks = 2^63  (= (usize::MAX / 2) + 1)
(last_n_blocks as usize) * 2  →  2^64 mod 2^64  =  0
difficulties.len() + 0 > 1000  →  false   ← guard never fires
```

**Post-bypass allocations**

After the guard is skipped, two separate `Vec` allocations are driven by `last_n_blocks`:

1. **`reorg_last_n_numbers`** (lines 237–247): when the client supplies a `start_hash` that does not match the canonical ancestor, the range `(min_block_number..start_block_number)` is collected. With `last_n_blocks = 2^63`, `min(start_block_number, last_n_blocks) = start_block_number`, so `min_block_number = 0` and the Vec holds `start_block_number` entries. [3](#0-2) 

2. **`last_n_numbers`** (lines 291–297): the condition `last_block_number - start_block_number <= last_n_blocks` is always `true` for any realistic chain height vs. `2^63`, so the server collects `(start_block_number..last_block_number)` — up to the full chain height. [4](#0-3) 

Both Vecs are then fed into `complete_headers`, which performs one DB lookup and one `VerifiableHeader` construction per entry: [5](#0-4) 

The constant is confirmed at 1000: [6](#0-5) 

---

### Impact Explanation

A single malicious `GetLastStateProof` P2P message with `last_n_blocks = 2^63` causes the server to:
- Allocate a `Vec<BlockNumber>` of up to `chain_height` entries (e.g., ~10 M entries × 8 bytes = ~80 MB for numbers alone).
- Execute `chain_height` synchronous RocksDB reads inside `complete_headers`.
- Allocate `chain_height` `VerifiableHeader` objects (each containing a full serialized header, uncles hash, extension, and MMR root).

On a mainnet node with millions of blocks this exhausts heap memory and/or saturates the I/O subsystem, crashing or hanging the light-client protocol handler. The attacker needs no credentials, no PoW, and no prior state — only a single well-formed P2P message.

---

### Likelihood Explanation

- The overflow value (`2^63`) is a legal `u64` and fits in the wire-format field with no protocol-level rejection.
- No other guard between message receipt and the allocations checks `last_n_blocks` for magnitude.
- Release builds are the standard deployment mode for CKB nodes; debug builds would panic on the overflow, masking the bug during development.
- The attack is stateless and repeatable: the peer can send the message continuously.

---

### Recommendation

Replace the unchecked multiplication with a saturating or checked variant, or validate `last_n_blocks` against the limit before the combined expression:

```rust
// Option A – explicit pre-check
if last_n_blocks as usize > constant::GET_LAST_STATE_PROOF_LIMIT / 2 {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}

// Option B – saturating arithmetic
if self.message.difficulties().len()
    .saturating_add((last_n_blocks as usize).saturating_mul(2))
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

---

### Proof of Concept

```rust
// Demonstrates the overflow and guard bypass (no chain needed)
#[test]
fn overflow_bypasses_limit_check() {
    let last_n_blocks: u64 = (usize::MAX / 2) as u64 + 1; // 2^63 on 64-bit
    let difficulties_len: usize = 0;
    let limit: usize = 1000;

    // Simulates the release-mode expression at line 201
    let check_value = difficulties_len.wrapping_add((last_n_blocks as usize).wrapping_mul(2));
    assert_eq!(check_value, 0, "overflow wraps to 0");
    assert!(
        !(check_value > limit),
        "guard does NOT fire — limit check bypassed"
    );

    // After bypass, last_n_numbers would collect up to chain_height entries
    // e.g. (0..10_000_000).collect::<Vec<u64>>() — 80 MB of block numbers alone
}
```

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L124-130)
```rust
    fn complete_headers(
        &self,
        positions: &mut Vec<u64>,
        last_hash: &packed::Byte32,
        numbers: &[BlockNumber],
    ) -> Result<Vec<packed::VerifiableHeader>, String> {
        let mut headers = Vec::new();
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L244-247)
```rust
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
