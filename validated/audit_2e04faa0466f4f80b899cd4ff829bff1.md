### Title
Missing Zero-Hash Guard for Multi-Target `assume_valid_targets` Permanently Bypasses Script Verification — (`ckb-bin/src/setup.rs`)

---

### Summary

The zero-hash sentinel check in `Setup::run()` that is meant to disable the `assume_valid_target` feature only fires when **exactly one** target is supplied. When a node operator passes a comma-separated multi-target list that includes the all-zeros hash (e.g. `--assume-valid-target 0x0000...0000,0xREALHASH`), the guard is silently skipped. The zero hash is then stored in the live `assume_valid_targets` list. Because no real block can ever carry an all-zeros hash, the zero entry is never consumed, causing `Switch::DISABLE_SCRIPT` to be returned for every block processed by the chain verifier — permanently bypassing script execution for the lifetime of the node process.

---

### Finding Description

**Root cause — `ckb-bin/src/setup.rs` lines 101–115**

```rust
if let Some(ref assume_valid_targets) = config.network.sync.assume_valid_targets
    && let Some(first_target) = assume_valid_targets.first()
    && assume_valid_targets.len() == 1          // ← guard only fires for single-target lists
{
    if first_target == &H256::from_slice(&[0; 32]).expect("...") {
        info!("Disable assume valid targets since assume_valid_targets is zero");
        config.network.sync.assume_valid_targets = None;
    }
}
```

The CLI explicitly supports comma-separated multi-target lists (parsed at lines 75–87):

```rust
concacate_targets
    .split(',')
    .map(|s| H256::from_str(&s[2..]))
    .collect::<Result<Vec<H256>, _>>()
```

When `len > 1`, the `len == 1` guard is never entered, so a zero hash in position 0 of the list is stored verbatim into `config.network.sync.assume_valid_targets`.

**Downstream effect — `chain/src/verify.rs` lines 216–238**

```rust
let block_hash: H256 = Into::<H256>::into(BlockView::hash(block));
if targets.first().eq(&Some(&block_hash)) {   // zero hash never matches any real block
    targets.remove(0);
}
if targets.is_empty() {
    assume_valid_targets.take();
    Switch::NONE                               // full verification
} else {
    Switch::DISABLE_SCRIPT                     // ← returned for every block, forever
}
```

Because `targets.first()` is always the zero hash and no block hash is ever all-zeros, the entry is never removed. `targets.is_empty()` is never true, so `Switch::DISABLE_SCRIPT` is returned unconditionally for every block the chain service processes.

**Block-downloader stall — `sync/src/synchronizer/mod.rs` lines 266–313**

The `can_start` safety mechanism that would otherwise clear the targets (when the best-known header is within 24 h of wall-clock) only fires in the `None` branch when the best-known header is **recent**. During IBD the best-known header is far in the past, so the safety mechanism does not trigger, and the downloader stays in `CanStart::AssumeValidNotFound` indefinitely.

---

### Impact Explanation

A node operator who passes `--assume-valid-target 0x0000...0000,0xREALHASH` (intending to combine the "disable" sentinel with a real checkpoint) will experience one of two outcomes depending on sync phase:

1. **During IBD (best-known header > 24 h old):** The block downloader is permanently stuck in `CanStart::AssumeValidNotFound`; no blocks are fetched and the node never advances past its current tip — effective denial of sync.
2. **Post-IBD or via relay protocol:** Blocks that arrive through the relay path are processed by `chain/src/verify.rs` with `Switch::DISABLE_SCRIPT`, meaning all lock/type scripts are skipped. The node silently accepts blocks containing invalid scripts, diverging from the canonical chain and potentially accepting double-spends or other invalid state transitions.

Both outcomes are silent — no error is logged, and the node appears to be running normally.

---

### Likelihood Explanation

The `--assume-valid-target` flag explicitly documents the zero-hash sentinel as a supported input to disable the feature. A node operator who wants to combine "disable the default target" with "also skip to a known checkpoint" would naturally write `0x0000...0000,0xCHECKPOINT`. The CLI parser accepts this without error. The likelihood is low-to-medium for operators who read the help text carefully, but the failure mode is completely silent and the workaround (restart without the zero hash) is non-obvious.

---

### Recommendation

Extend the zero-hash guard to iterate over **all** entries in the list, not just the single-entry case:

```rust
if let Some(ref mut targets) = config.network.sync.assume_valid_targets {
    targets.retain(|t| t != &H256::from_slice(&[0; 32]).unwrap());
    if targets.is_empty() {
        config.network.sync.assume_valid_targets = None;
        info!("Disable assume valid targets since all targets are zero");
    }
}
```

Alternatively, reject a zero hash anywhere in the list with an explicit `ExitCode::Cli` error and a clear message.

---

### Proof of Concept

```
ckb run --assume-valid-target \
  0x0000000000000000000000000000000000000000000000000000000000000000,\
  0xb72f4d9758a36a2f9d4b8aea5a11d232e3e48332b76ec350f0a375fac10317a4
```

1. `Setup::run()` parses two targets; `len == 2`, so the zero-hash guard at line 103 is skipped.
2. Both hashes are stored in `config.network.sync.assume_valid_targets`.
3. `SharedBuilder` initialises `assume_valid_targets` with both entries (the zero hash is not in the DB, so it is not filtered).
4. Every call to `ChainVerifier::verify_block` hits the `else { Switch::DISABLE_SCRIPT }` branch because `targets.first()` is always `0x0000...0000`.
5. The node either stalls in IBD or processes all blocks without script verification, silently accepting any script content. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** ckb-bin/src/setup.rs (L75-87)
```rust
        config.network.sync.assume_valid_targets = matches
            .get_one::<String>(cli::ARG_ASSUME_VALID_TARGET)
            .map(|concacate_targets| {
                concacate_targets
                    .split(',')
                    .map(|s| H256::from_str(&s[2..]))
                    .collect::<Result<Vec<H256>, _>>()
                    .map_err(|err| {
                        error!("Invalid assume valid target: {}", err);
                        ExitCode::Cli
                    })
            })
            .transpose()?; // Converts Result<Option<T>, E> to Option<Result<T, E>>
```

**File:** ckb-bin/src/setup.rs (L101-115)
```rust
        if let Some(ref assume_valid_targets) = config.network.sync.assume_valid_targets
            && let Some(first_target) = assume_valid_targets.first()
            && assume_valid_targets.len() == 1
        {
            if first_target == &H256::from_slice(&[0; 32]).expect("must parse Zero h256 successful")
            {
                info!("Disable assume valid targets since assume_valid_targets is zero");
                config.network.sync.assume_valid_targets = None;
            } else {
                info!(
                    "assume_valid_targets set to {:?}",
                    config.network.sync.assume_valid_targets
                );
            }
        }
```

**File:** chain/src/verify.rs (L214-238)
```rust
    ) -> VerifyResult {
        let switch: Switch = switch.unwrap_or_else(|| {
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
                        info!(
                            "CKB reached all assume_valid_targets, will do full verification now"
                        );
                        Switch::NONE
                    } else {
                        Switch::DISABLE_SCRIPT
                    }
                }
                None => Switch::NONE,
            }
        });
```

**File:** sync/src/synchronizer/mod.rs (L266-313)
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
```

**File:** shared/src/shared_builder.rs (L435-460)
```rust
        let assume_valid_targets = Arc::new(Mutex::new({
            let not_exists_targets: Option<Vec<H256>> =
                sync_config.assume_valid_targets.clone().map(|targets| {
                    targets
                        .iter()
                        .filter(|&target_hash| {
                            let exists = snapshot.block_exists(&target_hash.into());
                            if exists {
                                info!("assume-valid target 0x{} exists in local db", target_hash);
                            }
                            !exists
                        })
                        .cloned()
                        .collect::<Vec<H256>>()
                });

            if not_exists_targets
                .as_ref()
                .is_some_and(|targets| targets.is_empty())
            {
                info!("all assume-valid targets synchronized, enter full verification mode");
                None
            } else {
                not_exists_targets
            }
        }));
```
