### Title
Governance Sequence Skip Allows Permanent Blocking of Approved Governance Actions — (`lazer/contracts/cardano/lib/pyth/governance.ak`)

---

### Summary

The sequence check in `parse_and_verify_action` uses a strict-greater-than comparison (`seen_sequence < seen_sequence`) rather than a strict-next-in-sequence check. Any relayer who can observe multiple in-flight governance VAAs can front-run by submitting the highest-sequence VAA first, permanently invalidating all lower-sequence VAAs that were legitimately approved and queued.

---

### Finding Description

`parse_and_verify_action` enforces replay protection with:

```
expect governance.seen_sequence < seen_sequence
``` [1](#0-0) 

The check only requires the incoming sequence to be *greater than* the stored watermark. It does not require `seen_sequence == governance.seen_sequence + 1`. After a successful call, the state is updated to the new (potentially skipped) value:

```
Governance { ..governance, seen_sequence }
``` [2](#0-1) 

All VAAs must pass guardian quorum verification before reaching this check:

```
let VAA { body, .. } = vaa.parse_and_verify_prepared(vaa, guardians)
``` [3](#0-2) 

which calls `wormhole.has_quorum` internally:

```
expect wormhole.has_quorum(guardians, hash, signatures)
``` [4](#0-3) 

The attacker does **not** need to forge signatures. Wormhole VAAs are public; any observer can relay any legitimately signed VAA in any order. The attack is:

1. Governance emits VAAs at sequences N+1, N+2, …, N+1000, all signed by the guardian set.
2. Attacker observes all VAAs from the Wormhole gossip network.
3. Attacker submits VAA N+1000 first.
4. `seen_sequence` is updated to N+1000.
5. Every subsequent attempt to submit VAAs N+1 through N+999 fails the check `governance.seen_sequence < seen_sequence` (N+1000 is not < N+1 … N+999).
6. Those governance actions are permanently unexecutable.

The three governance action types that can be permanently blocked are `UpdateTrustedSigner`, `UpgradeSpendScript`, and `UpgradeWithdrawScript`: [5](#0-4) 

---

### Impact Explanation

An attacker can permanently prevent execution of any subset of approved governance actions — including contract upgrades and trusted-signer rotations — without any privileged access. The voted outcome is never executed, matching the scoped impact of governance manipulation.

---

### Likelihood Explanation

The precondition is that multiple governance VAAs for the Cardano chain are in flight simultaneously. This is uncommon in steady state but realistic during upgrade windows, emergency key rotations, or any governance campaign that batches multiple actions. The attacker role is an unprivileged relayer with read access to the Wormhole gossip network, which is public.

---

### Recommendation

Replace the greater-than check with a strict next-in-sequence check:

```
expect governance.seen_sequence + 1 == seen_sequence
```

This enforces that governance actions are executed in the exact order they were approved, making it impossible to skip intermediate VAAs.

---

### Proof of Concept

State-transition test (pseudocode):

```
// Initial state: seen_sequence = N
governance = Governance { seen_sequence: N, ... }

// Attacker submits VAA with sequence N+5 (skipping N+1..N+4)
(_, governance') = parse_and_verify_action(vaa_N5, governance, guardians)
// governance'.seen_sequence == N+5  ✓ (passes current check)

// Now attempt to submit legitimately approved VAA N+1
parse_and_verify_action(vaa_N1, governance', guardians)
// FAILS: governance'.seen_sequence (N+5) < N+1 is false → permanent block
``` [6](#0-5)

### Citations

**File:** lazer/contracts/cardano/lib/pyth/governance.ak (L17-24)
```text
pub type GovernanceAction {
  UpdateTrustedSigner(VerificationKey, Option<ValidityRange>)
  UpgradeSpendScript(ScriptHash)
  UpgradeWithdrawScript {
    new_script: ScriptHash,
    previous_validity: ValidityRange,
  }
}
```

**File:** lazer/contracts/cardano/lib/pyth/governance.ak (L33-33)
```text
  let VAA { body, .. } = vaa.parse_and_verify_prepared(vaa, guardians)
```

**File:** lazer/contracts/cardano/lib/pyth/governance.ak (L36-41)
```text
  let seen_sequence = u64.as_int(body.sequence)
  expect governance.seen_sequence < seen_sequence
  (
    parser.run(governance_action(), body.payload),
    Governance { ..governance, seen_sequence },
  )
```

**File:** lazer/contracts/cardano/lib/wormhole/vaa.ak (L55-55)
```text
  expect wormhole.has_quorum(guardians, hash, signatures)
```
