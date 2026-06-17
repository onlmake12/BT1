### Title
Out-of-Order Governance VAA Submission Permanently Skips Intermediate Governance Actions — (`target_chains/sui/contracts/sources/governance/governance.move`)

---

### Summary

`governance::execute_governance_instruction` enforces only `sequence > last_executed_governance_sequence`, not `sequence == last_executed + 1`. Because `verify_vaa` is a `public` function callable by anyone with a valid Wormhole VAA, an unprivileged attacker can submit a legitimate higher-sequence governance VAA before lower-sequence ones are processed, permanently making those lower-sequence VAAs unexecutable.

---

### Finding Description

The sequence guard in `execute_governance_instruction` is:

```move
assert!(sequence > state::get_last_executed_governance_sequence(pyth_state),
    E_CANNOT_EXECUTE_GOVERNANCE_ACTION_WITH_OBSOLETE_SEQUENCE_NUMBER);
state::set_last_executed_governance_sequence(&latest_only, pyth_state, sequence);
``` [1](#0-0) 

This allows any sequence `N > last` to be accepted, with no requirement that `N == last + 1`. The same pattern exists in `contract_upgrade::authorize_upgrade`: [2](#0-1) 

The entry point `verify_vaa` is `public` with no signer/capability restriction — any caller who holds a Wormhole-verified VAA object can invoke it: [3](#0-2) 

`state::set_last_executed_governance_sequence` unconditionally overwrites the stored value: [4](#0-3) 

There is no `consumed_vaas` check inside `execute_governance_instruction` or `verify_vaa` for governance VAAs — the only replay protection is the sequence monotonicity check itself.

---

### Impact Explanation

Once `last_executed_governance_sequence` is advanced to sequence `N`, every governance VAA with sequence `< N` is permanently rejected with `E_CANNOT_EXECUTE_GOVERNANCE_ACTION_WITH_OBSOLETE_SEQUENCE_NUMBER`. The governance actions encoded in those skipped VAAs (e.g., `set_data_sources`, `set_update_fee`, `set_governance_data_source`, contract upgrades) are permanently lost and can never be executed. New VAAs with sequence `> N` can still be issued, so total governance is not permanently bricked, but the skipped actions are irrecoverable without re-issuing them as new VAAs — which itself requires governance coordination and may be impossible if the skipped action was time-sensitive (e.g., an emergency data-source rotation).

---

### Likelihood Explanation

Wormhole VAAs are publicly observable. The Pyth governance emitter uses a single sequence counter across all target chains. If the governance system issues VAAs for multiple chains in quick succession (sequences 100–110, some for Ethereum, some for Sui), an attacker can observe a higher-sequence Sui-valid VAA (or a `target_chain_id == 0` global VAA) and submit it on Sui before lower-sequence pending Sui VAAs are relayed. The `target_chain_id` check in `governance_instruction::validate` only requires `target_chain_id == sui_chain_id || target_chain_id == 0`: [5](#0-4) 

This makes the precondition realistic whenever multiple governance VAAs are in-flight simultaneously.

---

### Recommendation

Replace the `sequence > last` check with a strict consecutive check:

```move
assert!(sequence == state::get_last_executed_governance_sequence(pyth_state) + 1,
    E_CANNOT_EXECUTE_GOVERNANCE_ACTION_WITH_OBSOLETE_SEQUENCE_NUMBER);
```

Apply the same fix to `contract_upgrade::authorize_upgrade`. This ensures governance VAAs must be processed in strict order, eliminating the ability to skip intermediate actions.

---

### Proof of Concept

State-transition test:
1. Initialize state with `last_executed_governance_sequence = 50`.
2. Obtain (or construct in test) a valid governance VAA with `sequence = 55`.
3. Call `verify_vaa` → `execute_governance_instruction` with sequence 55. Succeeds; state now has `last_executed_governance_sequence = 55`.
4. Attempt to call `execute_governance_instruction` with a VAA of sequence 51, 52, 53, or 54. Each aborts with `E_CANNOT_EXECUTE_GOVERNANCE_ACTION_WITH_OBSOLETE_SEQUENCE_NUMBER`.
5. The governance actions encoded in sequences 51–54 are permanently unexecutable.

### Citations

**File:** target_chains/sui/contracts/sources/governance/governance.move (L46-67)
```text
    public fun verify_vaa(
        pyth_state: &State,
        verified_vaa: VAA,
    ): WormholeVAAVerificationReceipt {
        state::assert_latest_only(pyth_state);

        let vaa_data_source = pyth::data_source::new((vaa::emitter_chain(&verified_vaa) as u64), vaa::emitter_address(&verified_vaa));

        // The emitter chain and address must correspond to the Pyth governance emitter chain and contract.
        assert!(
            pyth::state::is_valid_governance_data_source(pyth_state, vaa_data_source),
            E_INVALID_GOVERNANCE_DATA_SOURCE
        );

        let digest = vaa::digest(&verified_vaa);

        let sequence = vaa::sequence(&verified_vaa);

        let payload = vaa::take_payload(verified_vaa);

        WormholeVAAVerificationReceipt { payload, digest, sequence }
    }
```

**File:** target_chains/sui/contracts/sources/governance/governance.move (L82-87)
```text
        // Require that new sequence number is greater than last executed sequence number.
        assert!(sequence > state::get_last_executed_governance_sequence(pyth_state),
            E_CANNOT_EXECUTE_GOVERNANCE_ACTION_WITH_OBSOLETE_SEQUENCE_NUMBER);

        // Update latest executed sequence number to current one.
        state::set_last_executed_governance_sequence(&latest_only, pyth_state, sequence);
```

**File:** target_chains/sui/contracts/sources/governance/contract_upgrade.move (L53-58)
```text
        // Require that new sequence number is greater than last executed sequence number.
        assert!(sequence > state::get_last_executed_governance_sequence(pyth_state),
            E_CANNOT_EXECUTE_GOVERNANCE_ACTION_WITH_OBSOLETE_SEQUENCE_NUMBER);

        // Update latest executed sequence number to current one.
        state::set_last_executed_governance_sequence_unchecked(pyth_state, sequence);
```

**File:** target_chains/sui/contracts/sources/state.move (L248-250)
```text
    public(friend) fun set_last_executed_governance_sequence(_: &LatestOnly, s: &mut State, sequence: u64) {
        s.last_executed_governance_sequence = sequence;
    }
```

**File:** target_chains/sui/contracts/sources/governance/governance_instruction.move (L20-24)
```text
    fun validate(instruction: &GovernanceInstruction) {
        assert!(instruction.module_ == MODULE, E_INVALID_GOVERNANCE_MODULE);
        let target_chain_id = instruction.target_chain_id;
        assert!(target_chain_id == (wormhole::state::chain_id() as u64) || target_chain_id == 0, E_TARGET_CHAIN_MISMATCH);
    }
```
