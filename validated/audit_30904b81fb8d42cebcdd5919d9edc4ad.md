The code path is concrete and traceable. Let me lay out the analysis.

---

## Code Path Analysis

In `profile()` [1](#0-0) :

- `to` is **clamped** to `tip_number` via `std::cmp::min` (line 71)
- `from` is **only** clamped to a minimum of `1` via `std::cmp::max` — **no upper bound** (line 69)

When `from = N > tip_number`, the first call on line 73 becomes `process_range_block(..., 1..N)`, which iterates block numbers `1, 2, ..., N-1`. Once the iterator reaches `tip_number + 1`, the following chain executes:

```rust
snapshot.get_block_hash(index)          // returns None (block doesn't exist)
    .and_then(|hash| snapshot.get_block(&hash))  // still None
    .expect("read block from store");    // PANIC
``` [2](#0-1) 

This is an unconditional `expect` with no guard against out-of-range block numbers.

---

### Title
Unguarded `.expect()` in `process_range_block` panics when `--from` exceeds chain tip — (`ckb-bin/src/subcommand/replay.rs`)

### Summary
`ckb replay --profile --from N --to M` with `N > tip_number` causes an unconditional process panic due to a missing upper-bound clamp on `from` before it is passed to `process_range_block`.

### Finding Description
In `profile()`, `to` is clamped to `tip_number` but `from` is not. The first call `process_range_block(&shared, chain_controller.clone(), 1..from)` iterates all block numbers from `1` to `from - 1`. For any block number beyond the stored chain tip, `snapshot.get_block_hash(index)` returns `None`, and the `.expect("read block from store")` at line 112 panics unconditionally, crashing the process. [3](#0-2) 

### Impact Explanation
The process terminates with a Rust panic. Scope explicitly covers "Any local command line crash" (0–500 points). No data corruption occurs, but the operator's replay/profiling session is aborted with an unhandled panic rather than a clean error message.

### Likelihood Explanation
Any local user who can invoke `ckb replay` and supplies a `--from` value larger than the current chain tip (e.g., on a freshly synced or partially synced node) will reliably trigger this. No special privileges are required beyond the ability to run the `ckb` binary.

### Recommendation
Clamp `from` to `tip_number` the same way `to` is clamped:

```rust
let from = from.map(|v| std::cmp::max(1, v).min(tip_number)).unwrap_or(1);
``` [4](#0-3) 

Alternatively, replace `.expect(...)` in `process_range_block` with a graceful error return instead of a panic. [2](#0-1) 

### Proof of Concept
```bash
# Assume local chain tip is at block 1000
ckb replay --profile --from 9999 --to 10000
# process_range_block iterates 1..9999
# At index 1001: get_block_hash(1001) -> None -> .expect() -> PANIC
# thread 'main' panicked at 'read block from store', ckb-bin/src/subcommand/replay.rs:112
```

### Citations

**File:** ckb-bin/src/subcommand/replay.rs (L67-73)
```rust
fn profile(shared: Shared, chain_controller: ChainController, from: Option<u64>, to: Option<u64>) {
    let tip_number = shared.snapshot().tip_number();
    let from = from.map(|v| std::cmp::max(1, v)).unwrap_or(1);
    let to = to
        .map(|v| std::cmp::min(v, tip_number))
        .unwrap_or(tip_number);
    process_range_block(&shared, chain_controller.clone(), 1..from);
```

**File:** ckb-bin/src/subcommand/replay.rs (L109-112)
```rust
        let block = snapshot
            .get_block_hash(index)
            .and_then(|hash| snapshot.get_block(&hash))
            .expect("read block from store");
```
