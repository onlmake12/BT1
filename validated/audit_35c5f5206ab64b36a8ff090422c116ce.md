### Title
Block Assembler Lock Script Args Not Validated When `--ba-advanced` Is Used — (File: `util/launcher/src/lib.rs`)

---

### Summary

`sanitize_block_assembler_config` in `util/launcher/src/lib.rs` completely skips all lock script arg validation when the `--ba-advanced` CLI flag is set. The unvalidated `args` bytes are then used verbatim to construct the cellbase witness lock script. If a miner supplies wrong args (wrong length, all-zero pubkey hash, or a hash for which no private key exists), every subsequent block's mining reward is permanently locked to an unspendable cell. No warning or error is emitted.

---

### Finding Description

`sanitize_block_assembler_config` contains a short-circuit branch:

```rust
// util/launcher/src/lib.rs  lines 101-109
} else if self.args.block_assembler_advanced          // ← bypasses ALL checks
    || (block_assembler.hash_type == ScriptHashType::Type
        && block_assembler.args.len() == SECP256K1_BLAKE160_SIGHASH_ALL_ARG_LEN
        && check_lock_code_hash(&(&block_assembler.code_hash).into())?)
{
    Some(block_assembler)   // accepted with zero validation
``` [1](#0-0) 

When `block_assembler_advanced` is `true`, the three-part guard (`hash_type`, `args.len()`, `check_lock_code_hash`) is never evaluated. The `BlockAssemblerConfig` is returned as-is.

The accepted config is then passed to `BlockAssembler::build_cellbase_witness`, which writes `config.args` directly into the cellbase lock script:

```rust
// tx-pool/src/block_assembler/mod.rs  lines 494-499
let cellbase_lock = Script::new_builder()
    .args(config.args.as_bytes())   // ← no validation
    .code_hash(&config.code_hash)
    .hash_type(hash_type)
    .build();
``` [2](#0-1) 

That witness lock is later read back by `RewardCalculator::block_reward_to_finalize` to produce `target_lock`, which becomes the output lock of the finalized cellbase reward cell:

```rust
// tx-pool/src/block_assembler/mod.rs  lines 537-544
let (target_lock, block_reward) = block_in_place(|| {
    RewardCalculator::new(snapshot.consensus(), snapshot).block_reward_to_finalize(tip)
})?;
let output = CellOutput::new_builder()
    .capacity(block_reward.total)
    .lock(target_lock)          // ← derived from the unvalidated args
    .build();
``` [3](#0-2) 

`BlockAssemblerConfig.args` is typed as `JsonBytes` — a raw byte vector — with no length or content constraints enforced at the struct level:

```rust
// util/app-config/src/configs/tx_pool.rs  lines 55-63
pub struct BlockAssemblerConfig {
    pub code_hash: H256,
    pub args: JsonBytes,   // ← accepts any byte sequence
    ...
}
``` [4](#0-3) 

The `--ba-advanced` flag is parsed in `ckb-bin/src/setup.rs` and stored in `RunArgs::block_assembler_advanced`:

```rust
// ckb-bin/src/setup.rs  line 120
block_assembler_advanced: matches.get_flag(cli::ARG_BA_ADVANCED),
``` [5](#0-4) 

It is a supported, documented CLI flag advertised in the warning message emitted when standard validation fails:

```
"Edit ckb.toml or use `ckb run --ba-advanced` for other lock scripts"
``` [6](#0-5) 

---

### Impact Explanation

Every block mined while the wrong lock script args are active produces a cellbase reward cell locked to an address the miner cannot spend. CKB's UTXO-like Cell Model makes this permanent: there is no admin override, no recovery path, and no on-chain mechanism to redirect already-finalized reward cells. The financial loss scales linearly with the number of blocks mined before the misconfiguration is discovered. The miner receives no runtime warning or error — the node operates normally.

**Impact score: High** (irreversible loss of all mining rewards for the affected period).

---

### Likelihood Explanation

`--ba-advanced` is the prescribed path for any miner using a non-standard lock script (multisig, hardware wallet, custom script). A single-byte typo in a 20-byte hex pubkey hash, an off-by-one in the arg length, or copy-pasting a code hash instead of a lock arg are all realistic mistakes. The absence of any warning when `--ba-advanced` is active means the error is not caught until the miner notices missing rewards — potentially after many blocks.

**Likelihood score: Low-to-Medium** (requires explicit use of `--ba-advanced`, but the flag is commonly needed and the mistake is easy to make silently).

---

### Recommendation

1. **Emit a startup warning** whenever `--ba-advanced` is active, explicitly stating that lock script arg validation has been bypassed and that incorrect args will cause permanent loss of mining rewards.
2. **Apply basic sanity checks even under `--ba-advanced`**: verify that `args` is non-empty, that `code_hash` is not the all-zero hash, and that the `args` length is consistent with the declared `hash_type` where determinable.
3. Consider requiring an explicit acknowledgement flag (e.g., `--ba-advanced --ba-no-validate`) to make the bypass intentional rather than implicit.

---

### Proof of Concept

1. Initialize a CKB node directory: `ckb init --chain dev`.
2. Edit `ckb.toml` to set a `[block_assembler]` section with a valid `code_hash` for secp256k1-blake160 but with an all-zero or wrong-length `args`:
   ```toml
   [block_assembler]
   code_hash = "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8"
   hash_type = "type"
   args = "0x0000000000000000000000000000000000000000"  # nobody holds this key
   message = "0x"
   ```
3. Start the node with `ckb run --ba-advanced`. The node starts without any warning or error.
4. Start the miner with `ckb miner`. Blocks are mined normally.
5. After `finalization_delay_length` blocks, inspect the cellbase outputs via `get_block`. Each reward cell's lock script will have `args = 0x0000...0000` — permanently unspendable.
6. The miner has no private key for this lock arg and cannot recover the rewards.

The root cause is the unconditional short-circuit at `util/launcher/src/lib.rs` line 101 that skips all arg validation when `block_assembler_advanced` is `true`, combined with the direct use of those unvalidated args in `tx-pool/src/block_assembler/mod.rs` `build_cellbase_witness` at line 496.

### Citations

**File:** util/launcher/src/lib.rs (L101-109)
```rust
            } else if self.args.block_assembler_advanced
                || (block_assembler.hash_type == ScriptHashType::Type
                    && block_assembler.args.len() == SECP256K1_BLAKE160_SIGHASH_ALL_ARG_LEN
                    && check_lock_code_hash(&(&block_assembler.code_hash).into())?)
            {
                if block_assembler.use_binary_version_as_message_prefix {
                    block_assembler.binary_version = self.version.long();
                }
                Some(block_assembler)
```

**File:** util/launcher/src/lib.rs (L111-114)
```rust
                warn!(
                    "Miner is disabled because block assembler uses a non-recommended lock format. \
                     Edit ckb.toml or use `ckb run --ba-advanced` for other lock scripts"
                );
```

**File:** tx-pool/src/block_assembler/mod.rs (L494-499)
```rust
        let hash_type: ScriptHashType = config.hash_type.into();
        let cellbase_lock = Script::new_builder()
            .args(config.args.as_bytes())
            .code_hash(&config.code_hash)
            .hash_type(hash_type)
            .build();
```

**File:** tx-pool/src/block_assembler/mod.rs (L537-544)
```rust
            let (target_lock, block_reward) = block_in_place(|| {
                RewardCalculator::new(snapshot.consensus(), snapshot).block_reward_to_finalize(tip)
            })?;
            let input = CellInput::new_cellbase_input(candidate_number);
            let output = CellOutput::new_builder()
                .capacity(block_reward.total)
                .lock(target_lock)
                .build();
```

**File:** util/app-config/src/configs/tx_pool.rs (L55-63)
```rust
pub struct BlockAssemblerConfig {
    /// The miner lock script code hash.
    pub code_hash: H256,
    /// The miner lock script args.
    pub args: JsonBytes,
    /// An arbitrary message to be added into the cellbase transaction.
    pub message: JsonBytes,
    /// The miner lock script hash type.
    pub hash_type: ScriptHashType,
```

**File:** ckb-bin/src/setup.rs (L120-120)
```rust
            block_assembler_advanced: matches.get_flag(cli::ARG_BA_ADVANCED),
```
