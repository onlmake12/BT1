The code is confirmed. The `.unwrap()` at line 116 is real, and the `sanity_check` contrast at lines 141–150 is real.

Audit Report

## Title
Unwrap Panic in `process_range_block` Causes CLI Crash on Block Verification Failure — (`ckb-bin/src/subcommand/replay.rs`)

## Summary
`process_range_block` in `ckb-bin/src/subcommand/replay.rs` calls `.unwrap()` directly on the `VerifyResult` returned by `blocking_process_block_with_switch`. If any block in the replayed range returns `Err` under `Switch::NONE`, the process terminates with a Rust panic and raw backtrace instead of a structured error message. The `sanity_check` function in the same file already demonstrates the correct error-handling pattern, which `process_range_block` does not follow.

## Finding Description
In `process_range_block` (lines 101–119), every block is submitted with full verification (`Switch::NONE`) and the result is immediately unwrapped:

```rust
chain_controller
    .blocking_process_block_with_switch(Arc::new(block), Switch::NONE)
    .unwrap();   // line 116 — panics on Err
```

`blocking_process_block_with_switch` returns `VerifyResult = Result<bool, Error>`. Any block whose contextual verification fails returns `Err(...)`, and `.unwrap()` on that value causes a Rust panic — a process abort with exit code 101 and a raw backtrace, not a clean error.

The `sanity_check` function (lines 141–154) in the same file already handles this correctly:

```rust
if let Err(e) = chain_controller.blocking_process_block_with_switch(Arc::new(block), switch) {
    eprintln!("Replay sanity-check error: {:?} at block({}-{})", e, ...);
    pb.finish_with_message("replay finish");
    return;
}
```

`process_range_block` is called twice from `profile` (lines 73 and 76): once for the warm-up pass (`1..from`) and once for the measured pass (`from..=to`). Both calls are affected. [1](#0-0) [2](#0-1) 

## Impact Explanation
**Note (0–500 points): Any local command line crash.**

When `ckb replay --profile` is run against a database containing a block whose contextual verification fails under `Switch::NONE`, the process terminates with a Rust panic (exit code 101, raw backtrace) rather than a structured error message and clean exit. This violates the expected CLI contract for the `ckb replay` subcommand. [3](#0-2) 

## Likelihood Explanation
The trigger requires only a local CKB data directory containing at least one block that fails verification under `Switch::NONE`. This can occur with a database migrated from a different consensus configuration, a partially corrupted database, or blocks originally stored under a looser `Switch`. No network access, cryptographic material, or elevated privileges are required beyond normal filesystem ownership of the CKB data directory. The panic is deterministic: the first failing block in the range immediately triggers it.

## Recommendation
Replace the `.unwrap()` in `process_range_block` with explicit error handling mirroring `sanity_check`. Change the return type to `Result<usize, ckb_error::Error>` and use `?` to propagate errors. Update the callers in `profile` to print the error and return early, consistent with the rest of the CLI subcommand error-handling style. [4](#0-3) 

## Proof of Concept
1. Obtain a valid CKB data directory (mainnet or testnet snapshot).
2. Use a low-level RocksDB tool (e.g. `ldb`) to overwrite one transaction in a stored block so that its script verification fails under `Switch::NONE`.
3. Run: `ckb replay --profile --tmp-target /tmp/replay_tmp --config <path>`
4. **Expected (correct):** clean error message printed to stderr, exit code 1.
5. **Actual:** `thread 'main' panicked at 'called \`Result::unwrap()\` on an \`Err\` value: ...'`, exit code 101.

The panic is deterministic: every block in the range is processed with `Switch::NONE`, so the first block whose verification returns `Err` immediately triggers the unwrap. [5](#0-4)

### Citations

**File:** ckb-bin/src/subcommand/replay.rs (L53-57)
```rust
        if let Some((from, to)) = args.profile {
            profile(shared, chain_controller, from, to);
        } else if args.sanity_check {
            sanity_check(shared, chain_controller, args.full_verification);
        }
```

**File:** ckb-bin/src/subcommand/replay.rs (L73-76)
```rust
    process_range_block(&shared, chain_controller.clone(), 1..from);
    println!("Start profiling, re-process blocks {from}..{to}:");
    let now = std::time::Instant::now();
    let tx_count = process_range_block(&shared, chain_controller, from..=to);
```

**File:** ckb-bin/src/subcommand/replay.rs (L101-119)
```rust
fn process_range_block(
    shared: &Shared,
    chain_controller: ChainController,
    range: impl Iterator<Item = u64>,
) -> usize {
    let mut tx_count = 0;
    let snapshot = shared.snapshot();
    for index in range {
        let block = snapshot
            .get_block_hash(index)
            .and_then(|hash| snapshot.get_block(&hash))
            .expect("read block from store");
        tx_count += block.transactions().len().saturating_sub(1);
        chain_controller
            .blocking_process_block_with_switch(Arc::new(block), Switch::NONE)
            .unwrap();
    }
    tx_count
}
```

**File:** ckb-bin/src/subcommand/replay.rs (L141-154)
```rust
        if let Err(e) = chain_controller.blocking_process_block_with_switch(Arc::new(block), switch)
        {
            eprintln!(
                "Replay sanity-check error: {:?} at block({}-{})",
                e,
                header.number(),
                header.hash(),
            );
            pb.finish_with_message("replay finish");
            return;
        } else {
            pb.inc(1);
            cursor = header;
        }
```
