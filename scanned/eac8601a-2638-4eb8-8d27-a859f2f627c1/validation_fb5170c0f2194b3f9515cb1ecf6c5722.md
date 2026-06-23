### Title
Unwrap Panic in `process_range_block` Causes CLI Crash on Block Verification Failure — (`ckb-bin/src/subcommand/replay.rs`)

---

### Summary

The `process_range_block` function in the `ckb replay --profile` path calls `.unwrap()` directly on the `Result` returned by `blocking_process_block_with_switch`. If any block in the replayed range fails contextual verification (with `Switch::NONE`), the process panics instead of printing a clean error and exiting gracefully. The `sanity_check` path in the same file demonstrates the correct pattern but `process_range_block` does not follow it.

---

### Finding Description

In `ckb-bin/src/subcommand/replay.rs`, `process_range_block` is called by the `profile` function for both the warm-up pass and the measured pass:

```
profile()
  └─ process_range_block(&shared, chain_controller.clone(), 1..from);   // warm-up
  └─ process_range_block(&shared, chain_controller, from..=to);         // profiling
```

Inside `process_range_block`, every block is submitted with full verification (`Switch::NONE`) and the result is immediately unwrapped:

```rust
chain_controller
    .blocking_process_block_with_switch(Arc::new(block), Switch::NONE)
    .unwrap();   // panics on Err
```

`blocking_process_block_with_switch` returns `VerifyResult = Result<bool, Error>`. Any block that fails contextual verification returns `Err(...)`, and `.unwrap()` on that value causes a Rust panic — a process abort with a non-zero exit code and a backtrace dump, not a clean error message.

The `sanity_check` path in the same file already shows the correct idiom:

```rust
if let Err(e) = chain_controller.blocking_process_block_with_switch(Arc::new(block), switch) {
    eprintln!("Replay sanity-check error: {:?} at block({}-{})", e, ...);
    pb.finish_with_message("replay finish");
    return;
}
```

`process_range_block` never received the same treatment. [1](#0-0) [2](#0-1) 

---

### Impact Explanation

Scope: **Any local command line crash** (0–500 points).

When `ckb replay --profile` is run against a database that contains even one block whose contextual verification fails under `Switch::NONE`, the process terminates with a Rust panic rather than a structured error message. The exit is non-zero and the output is a raw panic backtrace, violating the invariant that the `ckb replay` CLI subcommand must handle verification errors gracefully. [3](#0-2) 

---

### Likelihood Explanation

The trigger condition is straightforward and locally reproducible:

1. A database whose stored blocks were originally accepted under a looser `Switch` (e.g. `Switch::DISABLE_SCRIPT`) but fail under `Switch::NONE` (full re-verification).
2. A database that has been partially corrupted or migrated from a different consensus configuration.
3. Any operator who crafts or obtains such a database and runs `ckb replay --profile`.

No network access, no cryptographic material, and no privileged system access beyond normal file-system ownership of the CKB data directory is required. [4](#0-3) [5](#0-4) 

---

### Recommendation

Replace the `.unwrap()` in `process_range_block` with explicit error handling that mirrors `sanity_check`:

```rust
fn process_range_block(
    shared: &Shared,
    chain_controller: ChainController,
    range: impl Iterator<Item = u64>,
) -> Result<usize, ckb_error::Error> {
    let mut tx_count = 0;
    let snapshot = shared.snapshot();
    for index in range {
        let block = snapshot
            .get_block_hash(index)
            .and_then(|hash| snapshot.get_block(&hash))
            .expect("read block from store");
        tx_count += block.transactions().len().saturating_sub(1);
        chain_controller
            .blocking_process_block_with_switch(Arc::new(block), Switch::NONE)?;
    }
    Ok(tx_count)
}
```

The callers in `profile` should propagate or print the error and return early, consistent with the rest of the CLI subcommand error-handling style. [6](#0-5) 

---

### Proof of Concept

```bash
# 1. Start with a valid CKB data directory (mainnet or testnet snapshot).
# 2. Use a low-level RocksDB tool (e.g. ldb) to overwrite one transaction
#    in a stored block so that its script verification will fail under Switch::NONE.
# 3. Run:
ckb replay --profile --tmp-target /tmp/replay_tmp --config <path>
# Expected (correct): clean error message printed to stderr, exit code 1
# Actual:             thread 'main' panicked at 'called `Result::unwrap()` on an `Err` value: ...'
#                     exit code 101 (SIGABRT / panic)
```

The panic is deterministic: every block in the `from..=to` range is processed with `Switch::NONE`, so the first block whose verification returns `Err` immediately triggers the unwrap. [7](#0-6) [8](#0-7)

### Citations

**File:** ckb-bin/src/subcommand/replay.rs (L53-54)
```rust
        if let Some((from, to)) = args.profile {
            profile(shared, chain_controller, from, to);
```

**File:** ckb-bin/src/subcommand/replay.rs (L67-99)
```rust
fn profile(shared: Shared, chain_controller: ChainController, from: Option<u64>, to: Option<u64>) {
    let tip_number = shared.snapshot().tip_number();
    let from = from.map(|v| std::cmp::max(1, v)).unwrap_or(1);
    let to = to
        .map(|v| std::cmp::min(v, tip_number))
        .unwrap_or(tip_number);
    process_range_block(&shared, chain_controller.clone(), 1..from);
    println!("Start profiling, re-process blocks {from}..{to}:");
    let now = std::time::Instant::now();
    let tx_count = process_range_block(&shared, chain_controller, from..=to);
    let duration = std::time::Instant::now().saturating_duration_since(now);
    if duration.as_secs() < MIN_PROFILING_TIME {
        println!(
            concat!(
                "----------------------------\n",
                r#"Profiling with too short time({:?}) is inaccurate and referential; it's recommended to modify"#,
                "\n",
                r#"parameters(--from, --to) to increase block range, to make profiling time is greater than "#,
                "{} seconds\n----------------------------",
            ),
            duration, MIN_PROFILING_TIME
        );
    }
    let tps = if duration.as_secs() == 0 {
        0
    } else {
        tx_count as u64 / duration.as_secs()
    };
    println!(
        "\n----------------------------\nEnd profiling, duration:{:?}, txs:{}, tps:{}\n----------------------------",
        duration, tx_count, tps
    );
}
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

**File:** chain/src/chain_controller.rs (L71-77)
```rust
    pub fn blocking_process_block_with_switch(
        &self,
        block: Arc<BlockView>,
        switch: Switch,
    ) -> VerifyResult {
        self.blocking_process_block_internal(block, Some(switch))
    }
```

**File:** chain/src/chain_controller.rs (L79-109)
```rust
    fn blocking_process_block_internal(
        &self,
        block: Arc<BlockView>,
        switch: Option<Switch>,
    ) -> VerifyResult {
        let (verify_result_tx, verify_result_rx) = ckb_channel::oneshot::channel::<VerifyResult>();

        let verify_callback = {
            move |result: VerifyResult| {
                if let Err(err) = verify_result_tx.send(result) {
                    error!(
                        "blocking send verify_result failed: {}, this shouldn't happen",
                        err
                    )
                }
            }
        };

        let lonely_block = LonelyBlock {
            block,
            switch,
            verify_callback: Some(Box::new(verify_callback)),
        };

        self.asynchronous_process_lonely_block(lonely_block);
        verify_result_rx.recv().unwrap_or_else(|err| {
            Err(InternalErrorKind::System
                .other(format!("blocking recv verify_result failed: {}", err))
                .into())
        })
    }
```
