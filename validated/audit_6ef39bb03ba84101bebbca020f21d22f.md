I need to verify the actual code behavior before rendering a verdict.

All six code claims check out against the actual source. Here is the verdict:

---

Audit Report

## Title
TOCTOU Race in `process_fetch_cmd`: `assume_valid_targets` Cleared Between Independent Lock Acquisitions Causes Unconditional Panic in BlockDownload Thread — (`sync/src/synchronizer/mod.rs`)

## Summary
`process_fetch_cmd` calls `can_start()`, which acquires and releases the `assume_valid_targets` mutex inside the `assume_valid_target_find` closure and returns `CanStart::AssumeValidNotFound`. The `AssumeValidNotFound` match arm then performs a second, independent lock acquisition and calls `.expect("assume valid target must exist")` unconditionally. Between the two acquisitions, the chain verification thread can call `assume_valid_targets.take()` setting the value to `None`, causing the `.expect()` to panic and permanently terminating the `BlockDownload` thread's IBD fetch loop.

## Finding Description
In `process_fetch_cmd` (`sync/src/synchronizer/mod.rs`), the `match self.can_start()` at line 121 invokes `can_start()`, which calls the `assume_valid_target_find` closure (lines 266–314). That closure acquires `shared.assume_valid_targets()` as a local `MutexGuard`, observes `Some([last_target])` with the target absent from `header_map` and the time condition not met (the `None` branch at line 297 where the timestamp check fails), leaves `self.can_start` as `AssumeValidNotFound`, and drops the guard — releasing the mutex.

`process_fetch_cmd` then enters the `CanStart::AssumeValidNotFound` arm (line 138). At lines 143–148 it performs a **second, independent** call to `shared.assume_valid_targets()`, acquiring a new `MutexGuard`. The `.expect("assume valid target must exist")` at line 148 is unconditional — it executes on every entry into this branch regardless of the logging condition at line 150.

Concurrently, `verify_block()` in `chain/src/verify.rs` (lines 216–227) acquires the same mutex, removes the last target entry via `targets.remove(0)`, finds `targets.is_empty()` true, and calls `assume_valid_targets.take()`, setting the `Option` to `None`.

If this chain-thread operation occurs between the two lock acquisitions in `process_fetch_cmd`, the second acquisition returns a guard over `None`. The chain `.as_ref().and_then(|targets| targets.first())` evaluates to `None`, and `.expect()` panics, unwinding the `BlockFetchCMD::run` loop (lines 236–250) and terminating the thread.

The `else` branch at lines 311–312 (`*flag = CanStart::Ready`) only guards against `None` being observed **inside** `can_start()` itself. It provides no protection for the second, separate lock acquisition in `process_fetch_cmd`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

## Impact Explanation
The panic unwinds the `BlockFetchCMD::run` loop (lines 236–250), terminating the thread that is solely responsible for IBD block fetching. Once terminated, no further IBD blocks are fetched and the node cannot make chain progress until the process is restarted. This matches **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**. [6](#0-5) 

## Likelihood Explanation
No attacker capability is required. The race is triggered naturally during normal mainnet IBD when the chain thread processes the last hardcoded `assume_valid_target` block concurrently with the sync thread's logging path. The default target list contains multiple mainnet checkpoints; the race applies specifically when the last target is being processed. The race window is narrow (two sequential mutex acquisitions with no atomic guarantee), but the condition is deterministically reached on every mainnet IBD and requires no special peer behavior.

## Recommendation
Eliminate the second lock acquisition in the `AssumeValidNotFound` branch of `process_fetch_cmd`. The `assume_valid_target` value needed for logging should be captured **inside** `can_start()` while the mutex is still held and returned alongside the `CanStart` variant (e.g., `AssumeValidNotFound(Byte32)`). Alternatively, the `AssumeValidNotFound` branch should re-check for `None` after the second acquisition and gracefully handle it (e.g., transition to `CanStart::Ready`) instead of calling `.expect()`.

## Proof of Concept
Spawn two threads sharing the same `Shared` instance initialized with `assume_valid_targets = Some([target_hash])` where `target_hash` is absent from `header_map` and the time condition is not met. Thread A calls `can_start()` (returns `AssumeValidNotFound`), then parks. Thread B calls `verify_block()` with the target block, triggering `targets.remove(0)` followed by `assume_valid_targets.take()` → `None`. Thread A resumes and enters the `AssumeValidNotFound` match arm, executing the second `shared.assume_valid_targets()` acquisition and calling `.expect()` on the now-`None` guard — assert panic occurs, thread terminates.

### Citations

**File:** sync/src/synchronizer/mod.rs (L121-121)
```rust
        match self.can_start() {
```

**File:** sync/src/synchronizer/mod.rs (L138-148)
```rust
            CanStart::AssumeValidNotFound => {
                let state = self.sync_shared.state();
                let shared = self.sync_shared.shared();
                let best_known = state.shared_best_header_ref();
                let number = best_known.number();
                let assume_valid_target: Byte32 = shared
                    .assume_valid_targets()
                    .as_ref()
                    .and_then(|targets| targets.first())
                    .map(Pack::pack)
                    .expect("assume valid target must exist");
```

**File:** sync/src/synchronizer/mod.rs (L236-250)
```rust
    fn run(&mut self, stop_signal: Receiver<()>) {
        loop {
            select! {
                recv(self.recv) -> msg => {
                    if let Ok(cmd) = msg {
                        self.process_fetch_cmd(cmd)
                    }
                }
                recv(stop_signal) -> _ => {
                    info!("BlockDownload received exit signal, exit now");
                    return;
                }
            }
        }
    }
```

**File:** sync/src/synchronizer/mod.rs (L266-314)
```rust
        let assume_valid_target_find = |flag: &mut CanStart| {
            let mut assume_valid_targets = shared.assume_valid_targets();
            if let Some(ref targets) = *assume_valid_targets {
                if targets.is_empty() {
                    assume_valid_targets.take();
                    *flag = CanStart::Ready;
                    return;
                }
                let first_target = targets
                    .first()
                    .expect("has checked targets is not empty, assume valid target must exist");
                match shared.header_map().get(&first_target.into()) {
                    Some(header) => {
                        if matches!(*flag, CanStart::FetchToTarget(fetch_target) if fetch_target == header.number())
                        {
                            // BlockFetchCMD has set the fetch target, no need to set it again
                        } else {
                            *flag = CanStart::FetchToTarget(header.number());
                            info!(
                                "assume valid target found in header_map; CKB will start fetch blocks to {:?} now",
                                header.number_and_hash()
                            );
                        }
                        // Blocks that are no longer in the scope of ibd must be forced to verify
                        if unix_time_as_millis().saturating_sub(header.timestamp()) < MAX_TIP_AGE {
                            assume_valid_targets.take();
                            warn!(
                                "the duration gap between 'assume valid target' and 'now' is less than 24h; CKB will ignore the specified assume valid target and do full verification from now on"
                            );
                        }
                    }
                    None => {
                        // Best known already not in the scope of ibd, it means target is invalid
                        if unix_time_as_millis()
                            .saturating_sub(state.shared_best_header_ref().timestamp())
                            < MAX_TIP_AGE
                        {
                            warn!(
                                "the duration gap between 'shared_best_header' and 'now' is less than 24h, but CKB haven't found the assume valid target in header_map; CKB will ignore the specified assume valid target and do full verification from now on"
                            );
                            *flag = CanStart::Ready;
                            assume_valid_targets.take();
                        }
                    }
                }
            } else {
                *flag = CanStart::Ready;
            }
        };
```

**File:** chain/src/verify.rs (L216-227)
```rust
            let mut assume_valid_targets = self.shared.assume_valid_targets();
            match *assume_valid_targets {
                Some(ref mut targets) => {
                    //
                    let block_hash: H256 = Into::<H256>::into(BlockView::hash(block));
                    if targets.first().eq(&Some(&block_hash)) {
                        targets.remove(0);
                        info!("CKB reached one assume_valid_target: 0x{}", block_hash);
                    }

                    if targets.is_empty() {
                        assume_valid_targets.take();
```

**File:** shared/src/shared.rs (L470-472)
```rust
    pub fn assume_valid_targets(&self) -> MutexGuard<'_, Option<Vec<H256>>> {
        self.assume_valid_targets.lock()
    }
```
