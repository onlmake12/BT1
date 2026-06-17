The code is clear and the logic is traceable. Let me confirm the exact lines and access control.

### Title
Governance Sequence Gap Allows Permanent Skipping of Critical Governance Actions - (`lazer/contracts/cardano/lib/pyth/governance.ak`)

### Summary

The sequence check in `parse_and_verify_action` uses a strictly-greater-than comparison (`<`) rather than requiring the next consecutive sequence number. Because `GovernanceAction` submissions have no submitter access control, any party holding a valid high-sequence VAA can submit it before a pending low-sequence VAA, permanently invalidating all intermediate governance messages with no recovery path.

### Finding Description

In `parse_and_verify_action`, the replay-prevention check is:

```
let seen_sequence = u64.as_int(body.sequence)
expect governance.seen_sequence < seen_sequence          // line 37
...
Governance { ..governance, seen_sequence }               // line 40
``` [1](#0-0) 

The check only requires `new_sequence > current_seen_sequence`. It does **not** require `new_sequence == current_seen_sequence + 1`. Once a VAA with sequence N is accepted, `seen_sequence` is set to N, and every VAA with sequence < N is permanently rejected.

The `update` validator's `spend` handler applies **no submitter access control** to the `GovernanceAction` path — only `PurgeExpiredWithdrawScripts` gates on `is_owner`:

```aiken
when action is {
  GovernanceAction(action) ->
    execute_governance_action(state, action, guardians)   // no is_owner check
  PurgeExpiredWithdrawScripts -> {
    expect is_owner
    ...
  }
}
``` [2](#0-1) 

Wormhole VAAs are public once signed by the guardian network. Any observer can relay any valid VAA to the contract in any order.

### Impact Explanation

An attacker who observes two valid, guardian-signed governance VAAs in the mempool (or on the Wormhole guardian API) — e.g., sequence 5 (`UpdateTrustedSigner` revoking a compromised key) and sequence 1000 (any other action) — can submit sequence 1000 first. After that transaction is confirmed, `seen_sequence = 1000`, and sequence 5 fails `1000 < 5` permanently. There is no recovery path short of a contract upgrade. Critical security actions (key revocation, script upgrades) can be permanently blocked.

### Likelihood Explanation

The precondition requires two valid VAAs with non-consecutive sequences to be simultaneously available. This is realistic: Pyth governance may issue multiple actions in a short window, and all signed VAAs are publicly accessible via the Wormhole guardian network. The attacker needs no privileged access — only the ability to submit a Cardano transaction.

### Recommendation

Replace the `<` comparison with a strict consecutive-sequence check:

```aiken
expect governance.seen_sequence + 1 == seen_sequence
```

This matches the design intent (ordered, gapless execution) and is consistent with how other Pyth target-chain contracts handle governance sequencing (e.g., the CosmWasm contract uses `vaa.sequence <= state.governance_sequence_number` as a rejection condition, implying sequential processing). [3](#0-2) 

### Proof of Concept

1. Obtain two valid guardian-signed governance VAAs targeting the Cardano contract: VAA-A with `sequence=5` (e.g., `UpdateTrustedSigner` revoking a compromised key) and VAA-B with `sequence=1000` (any valid action).
2. Submit VAA-B (`sequence=1000`) to the `update` validator. The check `0 < 1000` passes; `seen_sequence` is set to `1000`.
3. Attempt to submit VAA-A (`sequence=5`). The check `1000 < 5` fails — transaction aborts.
4. The trusted signer revocation (VAA-A) can never be executed. The compromised signer remains active indefinitely. [1](#0-0)

### Citations

**File:** lazer/contracts/cardano/lib/pyth/governance.ak (L36-41)
```text
  let seen_sequence = u64.as_int(body.sequence)
  expect governance.seen_sequence < seen_sequence
  (
    parser.run(governance_action(), body.payload),
    Governance { ..governance, seen_sequence },
  )
```

**File:** lazer/contracts/cardano/validators/pyth_state.ak (L63-80)
```text
    when action is {
      GovernanceAction(action) ->
        execute_governance_action(state, action, guardians)
      PurgeExpiredWithdrawScripts -> {
        expect is_owner
        state.new(
          state.home,
          Pyth {
            ..state.data,
            deprecated_withdraw_scripts: purge_expired_scripts(
              state.data.deprecated_withdraw_scripts,
              self.validity_range,
            ),
          },
          state.reference_script,
        )
      }
    }
```

**File:** target_chains/cosmwasm/contracts/pyth/src/contract.rs (L258-262)
```rust
    if vaa.sequence <= state.governance_sequence_number {
        Err(PythContractError::OldGovernanceMessage)?;
    } else {
        updated_config.governance_sequence_number = vaa.sequence;
    }
```
