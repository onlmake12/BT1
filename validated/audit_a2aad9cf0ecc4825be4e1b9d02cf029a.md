### Title
Missing Zero Check for `code_hash` in `BlockAssemblerConfig` When `--ba-advanced` Flag Is Used — (File: `util/launcher/src/lib.rs`)

---

### Summary

`sanitize_block_assembler_config` in `util/launcher/src/lib.rs` performs no validation on `BlockAssemblerConfig.code_hash` when the `--ba-advanced` CLI flag is set. A miner who configures `code_hash` as all-zeros (or any invalid hash) will mine blocks whose cellbase output is locked to a non-existent script, permanently and irrecoverably losing all mining rewards. The cellbase output is immutable once committed to a block, making this a one-way loss analogous to the immutable-variable zero-address bug in the reference report.

---

### Finding Description

In `util/launcher/src/lib.rs`, `sanitize_block_assembler_config` validates `code_hash` only along the non-advanced path:

```rust
} else if self.args.block_assembler_advanced          // ← advanced flag bypasses ALL checks
    || (block_assembler.hash_type == ScriptHashType::Type
        && block_assembler.args.len() == SECP256K1_BLAKE160_SIGHASH_ALL_ARG_LEN
        && check_lock_code_hash(&(&block_assembler.code_hash).into())?)
{
    // accepted unconditionally when block_assembler_advanced == true
    Some(block_assembler)
``` [1](#0-0) 

When `block_assembler_advanced` is `true`, the entire validation branch is short-circuited. No check is performed that `code_hash != H256::zero()`. The accepted `BlockAssemblerConfig` is then passed directly into `build_cellbase_witness`:

```rust
let cellbase_lock = Script::new_builder()
    .args(config.args.as_bytes())
    .code_hash(&config.code_hash)   // ← zero hash written verbatim
    .hash_type(hash_type)
    .build();
``` [2](#0-1) 

The resulting cellbase output is committed to every mined block. The `BlockAssemblerConfig.code_hash` field is declared as a plain `H256` with no runtime guard: [3](#0-2) 

The `--ba-advanced` flag is a documented, supported CLI option explicitly described as "Allow any block assembler code hash and args": [4](#0-3) 

---

### Impact Explanation

A cellbase output locked to a zero `code_hash` script cannot be spent. No script with `code_hash = 0x000…000` exists in the genesis block's system cells, so the lock can never be satisfied. Every block mined while this misconfiguration is active produces a permanently frozen reward. Because the cellbase is part of the committed block, the loss is irreversible — there is no mechanism to reclaim the funds after the fact.

---

### Likelihood Explanation

`--ba-advanced` is the standard workaround for any non-secp256k1 lock script (custom multisig, DAO-integrated locks, etc.). Miners experimenting with custom lock scripts, or copying a template config that leaves `code_hash` at its zero default, will silently activate this path. The node emits no warning and starts mining normally; the loss is only discovered when the miner attempts to spend the cellbase outputs.

---

### Recommendation

Add an explicit non-zero check for `code_hash` inside `sanitize_block_assembler_config` that applies regardless of the `block_assembler_advanced` flag:

```rust
if block_assembler.code_hash == H256::default() {
    warn!("Miner is disabled: block_assembler.code_hash must not be the zero hash.");
    return Ok(None);
}
```

This mirrors the pattern already used for the `assume_valid_target` zero-hash guard in `ckb-bin/src/setup.rs`: [5](#0-4) 

---

### Proof of Concept

1. Initialize a node: `ckb init --chain dev`.
2. Edit `ckb.toml` to set:
   ```toml
   [block_assembler]
   code_hash = "0x0000000000000000000000000000000000000000000000000000000000000000"
   args      = "0x"
   hash_type = "type"
   message   = "0x"
   ```
3. Start the node with `ckb run --ba-advanced`.
4. `sanitize_block_assembler_config` reaches the `block_assembler_advanced` branch at `util/launcher/src/lib.rs:101`, skips all validation, and returns `Some(block_assembler)` with `code_hash = 0x000…000`. [1](#0-0) 
5. `build_cellbase_witness` constructs a `Script` with `code_hash = 0x000…000` and embeds it in every cellbase. [2](#0-1) 
6. Every mined block's coinbase output is locked to a script that can never be satisfied. The mining rewards are permanently inaccessible.

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

**File:** tx-pool/src/block_assembler/mod.rs (L495-499)
```rust
        let cellbase_lock = Script::new_builder()
            .args(config.args.as_bytes())
            .code_hash(&config.code_hash)
            .hash_type(hash_type)
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

**File:** ckb-bin/src/cli.rs (L184-188)
```rust
            Arg::new(ARG_BA_ADVANCED)
                .long(ARG_BA_ADVANCED)
                .action(clap::ArgAction::SetTrue)
                .help("Allow any block assembler code hash and args"),
        )
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
