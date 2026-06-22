### Title
Missing Zero-Value Check on `compact_to_difficulty` Return Value Allows Forged Difficulty in MMR Header Digest Verification - (`util/types/src/utilities/merkle_mountain_range.rs`)

---

### Summary

`HeaderDigest::verify()` calls `compact_to_difficulty(start_compact_target)` to reconstruct the expected total difficulty for a range of blocks, but does not check whether the returned difficulty is zero. `compact_to_difficulty` explicitly returns `U256::zero()` when the compact target is zero or causes an overflow. When this happens, `total_difficulty_calculated` collapses to zero, and the integrity check `total_difficulty != total_difficulty_calculated` is trivially bypassed by a peer who sets `total_difficulty = 0` in the crafted `HeaderDigest`. The PoW engines in the same codebase already guard against this exact condition, but `HeaderDigest::verify()` does not.

---

### Finding Description

`compact_to_difficulty` in `util/types/src/utilities/difficulty.rs` is defined as:

```rust
pub fn compact_to_difficulty(compact: u32) -> U256 {
    let (target, overflow) = compact_to_target(compact);
    if target.is_zero() || overflow {
        return U256::zero();   // <-- silently returns 0
    }
    target_to_difficulty(&target)
}
``` [1](#0-0) 

The PoW engines (`EaglesongPowEngine` and `EaglesongBlake2bPowEngine`) both call `compact_to_target` directly and explicitly reject a zero or overflowing target:

```rust
let (block_target, overflow) = compact_to_target(header.raw().compact_target().into());
if block_target.is_zero() || overflow {
    return false;
}
``` [2](#0-1) [3](#0-2) 

However, `HeaderDigest::verify()` in the MMR light-client path calls the higher-level `compact_to_difficulty` wrapper and uses its return value directly without any zero check:

```rust
let block_difficulty = compact_to_difficulty(start_compact_target);
let total_difficulty_calculated = block_difficulty * blocks_count;
if total_difficulty != total_difficulty_calculated {
    // error
}
``` [4](#0-3) 

When `start_compact_target` is `0` (or any value that causes `compact_to_target` to return a zero mantissa, e.g., `0x123456`, `0x1003456`, `0x2000056`, `0x3000000`, `0x4000000`—all confirmed to produce a zero target in the test suite), `compact_to_difficulty` returns `U256::zero()`. Multiplying zero by `blocks_count` yields zero. A peer who also sets `total_difficulty = 0` in the `HeaderDigest` passes the check unconditionally. [5](#0-4) 

---

### Impact Explanation

`HeaderDigest::verify()` is the integrity check used by the CKB light client protocol server when verifying MMR proofs sent by peers. [6](#0-5) 

A malicious light-client server peer can craft a `HeaderDigest` with:
- `start_compact_target = 0` (or any zero-producing compact value)
- `end_compact_target = 0` (same epoch, so they must match)
- `total_difficulty = 0`

The verify function will accept this as valid because `compact_to_difficulty(0) = 0`, `0 * blocks_count = 0`, and `0 == 0`. This allows the peer to forge MMR proofs that claim an arbitrary block range has zero cumulative difficulty, undermining the light client's ability to verify chain work and enabling chain-tip spoofing attacks against light clients.

---

### Likelihood Explanation

The light client protocol server is reachable by any unprivileged sync peer. No special keys, majority hashpower, or privileged access are required. The attacker only needs to connect as a peer and send a crafted `HeaderDigest` with a zero compact target and zero total difficulty. The compact value `0` is trivially constructable.

---

### Recommendation

Add an explicit zero-difficulty guard in `HeaderDigest::verify()` immediately after calling `compact_to_difficulty`, mirroring the pattern already used in the PoW engines:

```rust
let block_difficulty = compact_to_difficulty(start_compact_target);
if block_difficulty.is_zero() {
    return Err("compact_target is invalid: results in zero difficulty".to_string());
}
let total_difficulty_calculated = block_difficulty * blocks_count;
if total_difficulty != total_difficulty_calculated { ... }
```

Alternatively, call `compact_to_target` directly and check `target.is_zero() || overflow` before proceeding, exactly as `EaglesongPowEngine::verify` does. [2](#0-1) 

---

### Proof of Concept

1. A malicious peer acting as a light-client server constructs a `HeaderDigest` where `start_compact_target = 0`, `end_compact_target = 0`, `start_epoch == end_epoch` (same epoch number), and `total_difficulty = U256::zero()`.
2. `HeaderDigest::verify()` is called on this digest.
3. Step 1 (block numbers): passes if `start_number <= end_number`.
4. Step 2 (epochs): passes since `start_epoch == end_epoch`.
5. Step 3 (difficulties): `compact_to_difficulty(0)` returns `U256::zero()` because `compact_to_target(0)` returns `(U256::zero(), false)` — zero target, no overflow. `total_difficulty_calculated = 0 * blocks_count = 0`. The check `total_difficulty != total_difficulty_calculated` evaluates to `0 != 0` which is `false`, so no error is returned.
6. `verify()` returns `Ok(())` for a `HeaderDigest` with a completely invalid compact target and zero total difficulty.
7. The light client accepts the forged MMR proof, believing the attacker-controlled chain segment has zero cumulative work. [7](#0-6) [1](#0-0) [8](#0-7)

### Citations

**File:** util/types/src/utilities/difficulty.rs (L80-86)
```rust
pub fn compact_to_difficulty(compact: u32) -> U256 {
    let (target, overflow) = compact_to_target(compact);
    if target.is_zero() || overflow {
        return U256::zero();
    }
    target_to_difficulty(&target)
}
```

**File:** pow/src/eaglesong.rs (L16-24)
```rust
        let (block_target, overflow) = compact_to_target(header.raw().compact_target().into());

        if block_target.is_zero() || overflow {
            debug!(
                "compact_target is invalid: {:#x}",
                header.raw().compact_target()
            );
            return false;
        }
```

**File:** pow/src/eaglesong_blake2b.rs (L20-28)
```rust
        let (block_target, overflow) = compact_to_target(header.raw().compact_target().into());

        if block_target.is_zero() || overflow {
            debug!(
                "compact_target is invalid: {:#x}",
                header.raw().compact_target()
            );
            return false;
        }
```

**File:** util/types/src/utilities/merkle_mountain_range.rs (L66-122)
```rust
impl HeaderDigest for packed::HeaderDigest {
    /// Verify the MMR header digest
    fn verify(&self) -> Result<(), String> {
        // 1. Check block numbers.
        let start_number: BlockNumber = self.start_number().into();
        let end_number: BlockNumber = self.end_number().into();
        if start_number > end_number {
            let errmsg = format!(
                "failed since the start block number is bigger than the end ([{start_number},{end_number}])"
            );
            return Err(errmsg);
        }

        // 2. Check epochs.
        let start_epoch: EpochNumberWithFraction = self.start_epoch().into();
        let end_epoch: EpochNumberWithFraction = self.end_epoch().into();
        let start_epoch_number = start_epoch.number();
        let end_epoch_number = end_epoch.number();
        if start_epoch != end_epoch
            && ((start_epoch_number > end_epoch_number)
                || (start_epoch_number == end_epoch_number
                    && start_epoch.index() > end_epoch.index()))
        {
            let errmsg = format!(
                "failed since the start epoch is bigger than the end ([{start_epoch:#},{end_epoch:#}])"
            );
            return Err(errmsg);
        }

        // 3. Check difficulties when in the same epoch.
        let start_compact_target: u32 = self.start_compact_target().into();
        let end_compact_target: u32 = self.end_compact_target().into();
        let total_difficulty: U256 = self.total_difficulty().into();
        if start_epoch_number == end_epoch_number {
            if start_compact_target != end_compact_target {
                // In the same epoch, all compact targets should be same.
                let errmsg = format!(
                    "failed since the compact targets should be same during epochs ([{start_epoch:#},{end_epoch:#}])"
                );
                return Err(errmsg);
            } else {
                // Sum all blocks difficulties to check total difficulty.
                let blocks_count = end_number - start_number + 1;
                let block_difficulty = compact_to_difficulty(start_compact_target);
                let total_difficulty_calculated = block_difficulty * blocks_count;
                if total_difficulty != total_difficulty_calculated {
                    let errmsg = format!(
                        "failed since total difficulty is {total_difficulty} but the calculated is {total_difficulty_calculated} \
                        during epochs ([{start_epoch:#},{end_epoch:#}])"
                    );
                    return Err(errmsg);
                }
            }
        }

        Ok(())
    }
```

**File:** util/types/src/utilities/tests/difficulty.rs (L54-84)
```rust
fn test_compact_convert() {
    let (ret, overflow) = compact_to_target(0);
    let compact = target_to_compact(u256!("0x0"));
    assert_eq!(ret, u256!("0x0"));
    assert!(!overflow);
    assert_eq!(compact, 0);

    let (ret, overflow) = compact_to_target(0x123456);
    assert_eq!(ret, u256!("0x0"));
    assert!(!overflow);

    let (ret, overflow) = compact_to_target(0x1003456);
    assert_eq!(ret, u256!("0x0"));
    assert!(!overflow);

    let (ret, overflow) = compact_to_target(0x2000056);
    assert_eq!(ret, u256!("0x0"));
    assert!(!overflow);

    let (ret, overflow) = compact_to_target(0x3000000);
    assert_eq!(ret, u256!("0x0"));
    assert!(!overflow);

    let (ret, overflow) = compact_to_target(0x4000000);
    assert_eq!(ret, u256!("0x0"));
    assert!(!overflow);

    let (ret, overflow) = compact_to_target(0x923456);
    assert_eq!(ret, u256!("0x0"));
    assert!(!overflow);

```
