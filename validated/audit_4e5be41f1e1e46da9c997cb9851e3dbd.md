### Title
Unconditional `.expect()` on Optional System Cell Type Hashes in `WellKnownScriptsOnlyValidator` Causes Node Panic on Custom Networks - (File: `rpc/src/module/pool.rs`)

---

### Summary

`WellKnownScriptsOnlyValidator` in `rpc/src/module/pool.rs` calls `.expect()` on `Option<Byte32>` values returned by `consensus.secp256k1_blake160_sighash_all_type_hash()` and `consensus.secp256k1_blake160_multisig_all_type_hash()`. These return `None` when the corresponding system cells are absent from the genesis block (e.g., on a custom or dev network). An unprivileged RPC caller can trigger this panic by submitting a transaction with `outputs_validator: WellKnownScriptsOnly` and a lock script using `hash_type: type`, crashing the RPC handler.

---

### Finding Description

`ConsensusBuilder::build()` populates `secp256k1_blake160_sighash_all_type_hash` and `secp256k1_blake160_multisig_all_type_hash` by calling `get_type_hash()`, which returns `Option<Byte32>` — `None` if the genesis cellbase output at the expected index has no type script: [1](#0-0) 

These fields are typed as `Option<Byte32>` in `Consensus`: [2](#0-1) 

And their accessors return `Option<Byte32>`: [3](#0-2) 

However, `validate_secp256k1_blake160_sighash_all` and `validate_secp256k1_blake160_multisig_all` call `.expect()` unconditionally on these `Option` values: [4](#0-3) [5](#0-4) 

The panic is reached whenever the lock script passes the `hash_type == type` check (line 791), which is controlled entirely by the attacker-supplied transaction.

The `validate_lock_script` method chains both validators: [6](#0-5) 

This is invoked from `send_transaction` and `test_tx_pool_accept` via `check_output_validator` when the caller passes `outputs_validator: WellKnownScriptsOnly`.

---

### Impact Explanation

On any CKB-compatible network whose genesis block omits the secp256k1 system cells (or sets `create_type_id = false` for them), the `secp256k1_blake160_sighash_all_type_hash` and `secp256k1_blake160_multisig_all_type_hash` fields are `None`. An unprivileged RPC caller submitting a transaction with `outputs_validator: WellKnownScriptsOnly` and any output whose lock script has `hash_type = type` will trigger an unconditional `.expect()` panic in the RPC handler thread. Depending on the server's panic handling, this causes the RPC handler to abort (denying service to that request) or, in configurations where panics abort the process, crashes the node entirely.

---

### Likelihood Explanation

CKB explicitly supports custom chain specs via the `ChainSpec` / `Genesis` system. The `ConsensusBuilder::default()` (used in tests and custom deployments) produces a genesis block with no system cells, yielding `None` for both hashes. Any operator deploying a custom CKB network without the standard secp256k1 system cells exposes their node to this panic from any RPC caller who sends a transaction with `outputs_validator: WellKnownScriptsOnly`. The trigger is a single well-formed RPC call requiring no special privileges.

---

### Recommendation

Replace the unconditional `.expect()` calls with graceful `Option` handling. If the system cell is absent, return a `DefaultOutputsValidatorError` variant (e.g., `NotWellKnownLockScript`) rather than panicking:

```rust
fn validate_secp256k1_blake160_sighash_all(
    &self,
    output: &packed::CellOutput,
) -> std::result::Result<(), DefaultOutputsValidatorError> {
    let script = output.lock();
    let expected_hash = self
        .consensus
        .secp256k1_blake160_sighash_all_type_hash()
        .ok_or(DefaultOutputsValidatorError::NotWellKnownLockScript)?;
    if !script.is_hash_type_type() {
        Err(DefaultOutputsValidatorError::HashType)
    } else if script.code_hash() != expected_hash {
        Err(DefaultOutputsValidatorError::CodeHash)
    } else if script.args().len() != BLAKE160_LEN {
        Err(DefaultOutputsValidatorError::ArgsLen)
    } else {
        Ok(())
    }
}
```

Apply the same pattern to `validate_secp256k1_blake160_multisig_all`.

---

### Proof of Concept

1. Start a CKB node with a custom chain spec whose genesis block does not include `secp256k1_blake160_sighash_all` with `create_type_id = true` (e.g., use `ConsensusBuilder::default()` or a stripped-down spec).
2. Confirm `consensus.secp256k1_blake160_sighash_all_type_hash()` returns `None`.
3. Submit via RPC:
```json
{
  "method": "send_transaction",
  "params": [
    {
      "version": "0x0",
      "cell_deps": [],
      "header_deps": [],
      "inputs": [{"previous_output": {"tx_hash": "0x...", "index": "0x0"}, "since": "0x0"}],
      "outputs": [{
        "capacity": "0x...",
        "lock": {
          "code_hash": "0xaaaa...aaaa",
          "hash_type": "type",
          "args": "0x"
        }
      }],
      "outputs_data": ["0x"],
      "witnesses": []
    },
    "passthrough_well_known_scripts_only"
  ]
}
```
4. The node panics at `rpc/src/module/pool.rs` line 797: `expect("No secp256k1_blake160_sighash_all system cell")`. [7](#0-6) [8](#0-7)

### Citations

**File:** spec/src/consensus.rs (L307-315)
```rust
    fn get_type_hash(&self, output_index: u64) -> Option<Byte32> {
        self.inner
            .genesis_block
            .transaction(0)
            .expect("Genesis must have cellbase")
            .output(output_index as usize)
            .and_then(|output| output.type_().to_opt())
            .map(|type_script| type_script.calc_script_hash())
    }
```

**File:** spec/src/consensus.rs (L355-359)
```rust
        self.inner.dao_type_hash = self.get_type_hash(OUTPUT_INDEX_DAO).unwrap_or_default();
        self.inner.secp256k1_blake160_sighash_all_type_hash =
            self.get_type_hash(OUTPUT_INDEX_SECP256K1_BLAKE160_SIGHASH_ALL);
        self.inner.secp256k1_blake160_multisig_all_type_hash =
            self.get_type_hash(OUTPUT_INDEX_SECP256K1_BLAKE160_MULTISIG_ALL);
```

**File:** spec/src/consensus.rs (L526-530)
```rust
    pub secp256k1_blake160_sighash_all_type_hash: Option<Byte32>,
    /// The secp256k1_blake160_multisig_all_type_hash
    ///
    /// [SECP256K1/multisig](https://github.com/nervosnetwork/rfcs/blob/master/rfcs/0024-ckb-genesis-script-list/0024-ckb-genesis-script-list.md#secp256k1multisig)
    pub secp256k1_blake160_multisig_all_type_hash: Option<Byte32>,
```

**File:** spec/src/consensus.rs (L641-650)
```rust
    pub fn secp256k1_blake160_sighash_all_type_hash(&self) -> Option<Byte32> {
        self.secp256k1_blake160_sighash_all_type_hash.clone()
    }

    /// The secp256k1_blake160_multisig_all_type_hash
    ///
    /// [SECP256K1/multisig](https://github.com/nervosnetwork/rfcs/blob/master/rfcs/0024-ckb-genesis-script-list/0024-ckb-genesis-script-list.md#secp256k1multisig)
    pub fn secp256k1_blake160_multisig_all_type_hash(&self) -> Option<Byte32> {
        self.secp256k1_blake160_multisig_all_type_hash.clone()
    }
```

**File:** rpc/src/module/pool.rs (L769-776)
```rust
    fn validate_lock_script(
        &self,
        output: &packed::CellOutput,
    ) -> std::result::Result<(), DefaultOutputsValidatorError> {
        self.validate_secp256k1_blake160_sighash_all(output)
            .or_else(|_| self.validate_secp256k1_blake160_multisig_all(output))
            .or_else(|_| self.validate_well_known_lock_scripts(output))
    }
```

**File:** rpc/src/module/pool.rs (L786-805)
```rust
    fn validate_secp256k1_blake160_sighash_all(
        &self,
        output: &packed::CellOutput,
    ) -> std::result::Result<(), DefaultOutputsValidatorError> {
        let script = output.lock();
        if !script.is_hash_type_type() {
            Err(DefaultOutputsValidatorError::HashType)
        } else if script.code_hash()
            != self
                .consensus
                .secp256k1_blake160_sighash_all_type_hash()
                .expect("No secp256k1_blake160_sighash_all system cell")
        {
            Err(DefaultOutputsValidatorError::CodeHash)
        } else if script.args().len() != BLAKE160_LEN {
            Err(DefaultOutputsValidatorError::ArgsLen)
        } else {
            Ok(())
        }
    }
```

**File:** rpc/src/module/pool.rs (L814-819)
```rust
        } else if script.code_hash()
            != self
                .consensus
                .secp256k1_blake160_multisig_all_type_hash()
                .expect("No secp256k1_blake160_multisig_all system cell")
        {
```
