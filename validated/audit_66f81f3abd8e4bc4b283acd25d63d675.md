### Title
Event-Based Script Verification Bypass via Default `assume_valid_target` During IBD — (`chain/src/verify.rs`, `sync/src/synchronizer/mod.rs`)

---

### Summary

CKB's `assume_valid_target` feature, **enabled by default** on both mainnet and testnet, implements an event-based authorization bypass for script verification during Initial Block Download (IBD). The bypass is controlled by a mutable in-memory state (`assume_valid_targets: Arc<Mutex<Option<Vec<H256>>>>`) that is populated from peer-supplied headers. All lock scripts — the sole authorization mechanism for cell spending — are skipped for every block until the target is reached. A malicious sync peer that can present a chain with sufficient total difficulty can serve blocks containing unauthorized cell spending that the victim node accepts without any script execution.

---

### Finding Description

CKB's `verify_block` function in `chain/src/verify.rs` determines whether to run scripts by checking a mutable in-memory list of target block hashes:

```rust
let switch: Switch = switch.unwrap_or_else(|| {
    let mut assume_valid_targets = self.shared.assume_valid_targets();
    match *assume_valid_targets {
        Some(ref mut targets) => {
            let block_hash: H256 = Into::<H256>::into(BlockView::hash(block));
            if targets.first().eq(&Some(&block_hash)) {
                targets.remove(0);
            }
            if targets.is_empty() {
                assume_valid_targets.take();
                Switch::NONE
            } else {
                Switch::DISABLE_SCRIPT   // ← scripts skipped for ALL blocks before last target
            }
        }
        None => Switch::NONE,
    }
});
``` [1](#0-0) 

This `Switch::DISABLE_SCRIPT` flag is passed into `ContextualTransactionVerifier::verify`, which then skips the entire `ScriptVerifier` execution:

```rust
let cycles = if skip_script_verify {
    0
} else {
    self.script.verify(max_cycles)?
};
``` [2](#0-1) 

The decision to enter this skip mode is triggered by the synchronizer's `can_start` logic, which looks up the target hash in the `header_map` — a structure populated entirely from peer-supplied headers that have passed only PoW and non-contextual structural checks:

```rust
match shared.header_map().get(&first_target.into()) {
    Some(header) => {
        *flag = CanStart::FetchToTarget(header.number());
        // ...
    }
    // ...
}
``` [3](#0-2) 

The `header_map` is populated via `insert_valid_header`, which inserts headers after only `HeaderVerifier` (PoW + non-contextual) checks pass — no script execution is involved: [4](#0-3) 

This feature is **enabled by default** for both mainnet and testnet. For testnet, the default list contains **20 hardcoded target hashes** spanning up to height ~20,705,983: [5](#0-4) 

The targets are loaded at startup without any explicit user opt-in: [6](#0-5) 

The `assume_valid_targets` state is a plain mutex-protected list. The transition from "skip scripts" to "full verification" is purely event-based: it fires only when the last target block hash is matched. There is no cryptographic binding between the skip decision and the actual validity of the blocks being accepted.

---

### Impact Explanation

A node in IBD that accepts a peer-supplied chain with more total difficulty than the canonical chain will commit all blocks before the last assume_valid_target to its database **without executing any lock scripts**. This means:

- Cells can be spent without satisfying their lock script (e.g., without a valid secp256k1 signature).
- The node's UTXO set (live cell set) becomes permanently corrupted with unauthorized state transitions.
- Any application (wallet, indexer, exchange) relying on this node's chain state will operate on invalid data.
- The corrupted state is persisted to RocksDB and survives restarts.

---

### Likelihood Explanation

**Testnet (higher likelihood):** `min_chain_work` is hardcoded to `u256!("0x0")` for all non-mainnet chains: [7](#0-6) 

There is no minimum work barrier. An attacker with modest hashpower can:
1. Build a chain from genesis (or from a fork point before the first target) with invalid scripts but valid PoW.
2. Ensure the chain includes the real canonical target block hashes (by incorporating the public canonical chain data up to each target, then forking with invalid transactions before those checkpoints).
3. Accumulate more total difficulty than the canonical chain for the forked segment.
4. Serve this chain to a new testnet node during IBD.

The 24-hour timestamp guard (`MAX_TIP_AGE`) only applies to the target block's timestamp, not to the blocks being served. Since the hardcoded targets are ~60 days old, their timestamps are well outside the 24-hour window, so the guard does not trigger.

**Mainnet (lower likelihood):** `MIN_CHAIN_WORK_500K` is enforced, requiring the attacker's chain to meet a minimum accumulated difficulty. This is a significant barrier but does not eliminate the risk for well-resourced attackers.

---

### Recommendation

1. **Do not enable `assume_valid_target` by default.** Require explicit operator opt-in via CLI flag or config, with a prominent warning. The original CHANGELOG entry for this feature stated *"Please know exactly what you are doing before you use it!"* — this intent is violated by the current default-on behavior.

2. **Bind the skip decision to a cryptographic commitment.** Rather than a mutable in-memory list, consider committing to the assume_valid chain segment via a hardcoded total-difficulty threshold (analogous to `min_chain_work`) so that the skip cannot be triggered by a low-work attacker chain.

3. **Apply the 24-hour timestamp guard to the blocks being downloaded**, not just the target header, to prevent serving of stale forked chains.

4. **Audit the multi-target transition logic.** When multiple targets are configured, the block matching the first target is itself processed with `Switch::DISABLE_SCRIPT` (scripts disabled), because `targets.is_empty()` is false after removal. The checkpoint block is thus never fully verified. [8](#0-7) 

---

### Proof of Concept

**Testnet attack (no min_chain_work barrier):**

1. Attacker downloads the canonical testnet chain up to height N (before the first assume_valid_target at height 500,000).
2. Attacker creates a fork at height N with a transaction that spends a cell without a valid lock script (e.g., empty witness on a secp256k1 cell).
3. Attacker mines this fork with valid PoW until its total difficulty exceeds the canonical chain's total difficulty at the same height.
4. Attacker runs a CKB testnet node serving this forked chain.
5. Victim starts a fresh CKB testnet node and connects to the attacker's node.
6. Victim node syncs headers, finds the first assume_valid_target hash in its `header_map` (served by the attacker), enters `CanStart::FetchToTarget` mode.
7. Victim node downloads blocks from the attacker with `Switch::DISABLE_SCRIPT` active — no lock scripts are executed.
8. The invalid transaction (unauthorized cell spend) is committed to the victim's RocksDB.
9. After the last target is passed, full verification resumes — but the corrupted state is already persisted.

The victim node now reports an incorrect live cell set, with cells marked as spent that were never legitimately authorized.

### Citations

**File:** chain/src/verify.rs (L215-238)
```rust
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

**File:** verification/src/transaction_verifier.rs (L162-172)
```rust
    pub fn verify(&self, max_cycles: Cycle, skip_script_verify: bool) -> Result<Completed, Error> {
        self.time_relative.verify()?;
        self.capacity.verify()?;
        let cycles = if skip_script_verify {
            0
        } else {
            self.script.verify(max_cycles)?
        };
        let fee = self.fee_calculator.transaction_fee()?;
        Ok(Completed { cycles, fee })
    }
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

**File:** sync/src/types/mod.rs (L1094-1141)
```rust
    pub fn insert_valid_header(&self, peer: PeerIndex, header: &core::HeaderView) {
        let tip_number = self.active_chain().tip_number();
        let store_first = tip_number >= header.number();
        // We don't use header#parent_hash clone here because it will hold the arc counter of the SendHeaders message
        // which will cause the 2000 headers to be held in memory for a long time
        let parent_hash = Byte32::from_slice(header.data().raw().parent_hash().as_slice())
            .expect("checked slice length");
        let parent_header_index = self
            .get_header_index_view(&parent_hash, store_first)
            .expect("parent should be verified");
        let mut header_view = HeaderIndexView::new(
            header.hash(),
            header.number(),
            header.epoch(),
            header.timestamp(),
            parent_hash,
            parent_header_index.total_difficulty() + header.difficulty(),
        );

        let snapshot = Arc::clone(&self.shared.snapshot());
        header_view.build_skip(
            tip_number,
            |hash, store_first| self.get_header_index_view(hash, store_first),
            |number, current| {
                // shortcut to return an ancestor block
                if current.number <= snapshot.tip_number() && snapshot.is_main_chain(&current.hash)
                {
                    snapshot
                        .get_block_hash(number)
                        .and_then(|hash| self.get_header_index_view(&hash, true))
                } else {
                    None
                }
            },
        );
        self.shared.header_map().insert(header_view.clone());
        self.state
            .peers()
            .may_set_best_known_header(peer, header_view.as_header_index());
        if header_view.number().is_multiple_of(10000) {
            info!(
                "inserted valid header: header {}-{}",
                header_view.number(),
                header_view.hash()
            );
        }
        self.state.may_set_shared_best_header(header_view);
    }
```

**File:** util/constant/src/default_assume_valid_target.rs (L53-102)
```rust
/// testnet
pub mod testnet {
    use crate::latest_assume_valid_target;

    /// get testnet related default assume valid targets
    pub fn default_assume_valid_targets() -> Vec<&'static str> {
        vec![
            // height: 500000; https://testnet.explorer.nervos.org/block/0xf9c73f3db9a7c6707c3c6800a9a0dbd5a2edf69e3921832f65275dcd71f7871c
            "0xf9c73f3db9a7c6707c3c6800a9a0dbd5a2edf69e3921832f65275dcd71f7871c",
            // height: 1000000; https://testnet.explorer.nervos.org/block/0x935a48f2660fd141121114786edcf17ef5789c6c2fe7aca04ea27813b30e1fa3
            "0x935a48f2660fd141121114786edcf17ef5789c6c2fe7aca04ea27813b30e1fa3",
            // height: 2000000; https://testnet.explorer.nervos.org/block/0xf4d1648131b7bc4a0c9dbc442d240395c89a0c77b0cc197dce8794cd93669b32
            "0xf4d1648131b7bc4a0c9dbc442d240395c89a0c77b0cc197dce8794cd93669b32",
            // height: 3000000; https://testnet.explorer.nervos.org/block/0x1d1bd2a6a50d9532b7131c5d0b05c006fb354a0341a504e54eaf39b27acc620d
            "0x1d1bd2a6a50d9532b7131c5d0b05c006fb354a0341a504e54eaf39b27acc620d",
            // height: 4000000; https://testnet.explorer.nervos.org/block/0xb33c0e0a649003ab65062e93a3126a2235f6e7c3ca1b16fe9938816d846bb14f
            "0xb33c0e0a649003ab65062e93a3126a2235f6e7c3ca1b16fe9938816d846bb14f",
            // height: 5000000; https://testnet.explorer.nervos.org/block/0xff4f979d8ab597a5836c533828d5253021c05f2614470fd8a4df7724ff8ec5e1
            "0xff4f979d8ab597a5836c533828d5253021c05f2614470fd8a4df7724ff8ec5e1",
            // height: 6000000; https://testnet.explorer.nervos.org/block/0xfdb427f18e03cee68947609db1f592ee2651181528da35fb62b64d4d4d5d749a
            "0xfdb427f18e03cee68947609db1f592ee2651181528da35fb62b64d4d4d5d749a",
            // height: 7000000; https://testnet.explorer.nervos.org/block/0xf9e1c6398f524c10b358dca7e000f59992004fda68c801453ed4da06bc3c6ecc
            "0xf9e1c6398f524c10b358dca7e000f59992004fda68c801453ed4da06bc3c6ecc",
            // height: 8000000; https://testnet.explorer.nervos.org/block/0x2be0f327e78032f495f90da159883da84f2efd5025fde106a6a7590b8fca6647
            "0x2be0f327e78032f495f90da159883da84f2efd5025fde106a6a7590b8fca6647",
            // height: 9000000; https://testnet.explorer.nervos.org/block/0xba1e8db7d162445979f2c73392208b882ea01c7627a8a98be82789d6f130ce35
            "0xba1e8db7d162445979f2c73392208b882ea01c7627a8a98be82789d6f130ce35",
            // height: 10000000; https://testnet.explorer.nervos.org/block/0xf64c95cfa813e0aa1ae2e0e28af4723134263c9862979c953842511381b7d8c6
            "0xf64c95cfa813e0aa1ae2e0e28af4723134263c9862979c953842511381b7d8c6",
            // height: 11000000; https://testnet.explorer.nervos.org/block/0x0a9e4de75031163fefc5e7c0d40adadb2d7cb23eb9b1b2dae46872e921f4bcf1
            "0x0a9e4de75031163fefc5e7c0d40adadb2d7cb23eb9b1b2dae46872e921f4bcf1",
            // height: 12000000; https://testnet.explorer.nervos.org/block/0x9f24177a181798b7ad63dfc8e0b89fe0ce60c099e86743675070f428ca1037b4
            "0x9f24177a181798b7ad63dfc8e0b89fe0ce60c099e86743675070f428ca1037b4",
            // height: 13000000; https://testnet.explorer.nervos.org/block/0xc884fb5ca8cc2acddf6ce4888dc7fe0f583bb0dd4f80c5be31bed87268b1ca2f
            "0xc884fb5ca8cc2acddf6ce4888dc7fe0f583bb0dd4f80c5be31bed87268b1ca2f",
            // height: 14000000; https://testnet.explorer.nervos.org/block/0xfb7da0ff926540463e3a9168cf0cd73113c24e4692a561525554c87c62aa3475
            "0xfb7da0ff926540463e3a9168cf0cd73113c24e4692a561525554c87c62aa3475",
            // height: 15000000; https://testnet.explorer.nervos.org/block/0x0fbed5e1204d0a8352e6a1e4af5b7a0d1919f5242aa4d966657c23c969f1f79d
            "0x0fbed5e1204d0a8352e6a1e4af5b7a0d1919f5242aa4d966657c23c969f1f79d",
            // height: 16000000; https://testnet.explorer.nervos.org/block/0xa05ebcfd2f2a2b4bda1da4d48009eaab286d0511836c177fa49a605f242a2c4e
            "0xa05ebcfd2f2a2b4bda1da4d48009eaab286d0511836c177fa49a605f242a2c4e",
            // height: 17000000; https://testnet.explorer.nervos.org/block/0x6b9cec6c625b6369d7896a0290aed9a816b3d543e2fb7121043b6a358f2e54c4
            "0x6b9cec6c625b6369d7896a0290aed9a816b3d543e2fb7121043b6a358f2e54c4",
            // height: 18000000; https://testnet.explorer.nervos.org/block/0x28d608e264af05428843b1f5cc7ef582e4fc390e57b84011ea0454a4e1ca40eb
            "0x28d608e264af05428843b1f5cc7ef582e4fc390e57b84011ea0454a4e1ca40eb",
            // height: 19000000; https://testnet.explorer.nervos.org/block/0x5b390768dd2d515a5937c2595c7cb46c7b8d3174d86af47e59e2f5f393ec603a
            "0x5b390768dd2d515a5937c2595c7cb46c7b8d3174d86af47e59e2f5f393ec603a",
            latest_assume_valid_target::testnet::DEFAULT_ASSUME_VALID_TARGET,
        ]
    }
```

**File:** ckb-bin/src/setup.rs (L69-74)
```rust
        config.network.sync.min_chain_work =
            if consensus.genesis_block.hash() == mainnet_genesis.hash() {
                MIN_CHAIN_WORK_500K
            } else {
                u256!("0x0")
            };
```

**File:** ckb-bin/src/setup.rs (L89-99)
```rust
        if config.network.sync.assume_valid_targets.is_none() {
            config.network.sync.assume_valid_targets = match consensus.id.as_str() {
                ckb_constant::hardfork::mainnet::CHAIN_SPEC_NAME => Some(
                    ckb_constant::default_assume_valid_target::mainnet::default_assume_valid_targets().iter().map(|target|
H256::from_str(&target[2..]).expect("default assume_valid_target for mainnet must be valid")).collect::<Vec<H256>>()),
                ckb_constant::hardfork::testnet::CHAIN_SPEC_NAME => Some(
                    ckb_constant::default_assume_valid_target::testnet::default_assume_valid_targets().iter().map(|target|
H256::from_str(&target[2..]).expect("default assume_valid_target for testnet must be valid")).collect::<Vec<H256>>()),
                    _ => None,
            };
        }
```
