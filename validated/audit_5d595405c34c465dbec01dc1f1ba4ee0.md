### Title
Permissionless `advanceProviderCommitment()` Allows Any Caller to Front-Run Keeper Transactions and Advance Any Provider's Commitment State - (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

`Entropy.advanceProviderCommitment()` is a `public` function with **no `msg.sender == provider` check**. Any unprivileged caller who observes a valid `providerContribution` value in the mempool can front-run the Fortuna keeper's pending `advanceProviderCommitment` transaction, causing the keeper's transaction to revert with `UpdateTooOld`. Persistent front-running prevents the keeper from ever successfully advancing the commitment pointer, which — if `maxNumHashes` is set — eventually causes all new randomness requests to revert with `LastRevealedTooOld`, DoS-ing the Entropy service for that provider.

---

### Finding Description

`advanceProviderCommitment` is declared `public override` with no caller restriction:

```solidity
function advanceProviderCommitment(
    address provider,
    uint64 advancedSequenceNumber,
    bytes32 providerContribution
) public override {
```

The only validation is a hash-chain proof: `constructProviderCommitment(numHashes, providerContribution) == providerInfo.currentCommitment`. This proof is **data-dependent, not identity-dependent** — anyone who possesses a valid `providerContribution` can call the function for any `provider`.

The Fortuna keeper (`apps/fortuna/src/keeper/commitment.rs`) periodically calls `advanceProviderCommitment` to advance `currentCommitmentSequenceNumber` and reduce `numHashes` for future requests. The keeper's pending transaction is visible in the public mempool and contains the `providerContribution` value in plaintext calldata.

An attacker who monitors the mempool can:
1. Extract `providerContribution = x_seqNum` from the keeper's pending transaction.
2. Optionally compute lower-index chain values: `x_{seqNum-k} = hash^k(x_seqNum)`.
3. Submit `advanceProviderCommitment(provider, seqNum, x_seqNum)` with higher gas, front-running the keeper.
4. The keeper's transaction reverts with `UpdateTooOld` because `advancedSequenceNumber <= providerInfo.currentCommitmentSequenceNumber` after the attacker's transaction lands.

The attacker can repeat this every time the keeper retries, indefinitely.

**Secondary impact — `sequenceNumber` bump via the side-effect branch:**

```solidity
if (
    providerInfo.currentCommitmentSequenceNumber >=
    providerInfo.sequenceNumber
) {
    providerInfo.sequenceNumber =
        providerInfo.currentCommitmentSequenceNumber + 1;
}
```

If the attacker can supply a `providerContribution` for `advancedSequenceNumber >= providerInfo.sequenceNumber`, the contract permanently skips those sequence numbers. While this requires knowing a future chain value (not obtainable from the keeper's mempool transaction alone), it becomes reachable if the provider ever inadvertently reveals a future value (e.g., via a misconfigured re-registration or a `revealWithCallback` that exposes a higher-index value).

**DoS path via `maxNumHashes`:**

```solidity
req.numHashes = SafeCast.toUint32(
    assignedSequenceNumber -
        providerInfo.currentCommitmentSequenceNumber
);
if (
    providerInfo.maxNumHashes != 0 &&
    req.numHashes > providerInfo.maxNumHashes
) {
    revert EntropyErrors.LastRevealedTooOld();
}
```

If the keeper cannot advance `currentCommitmentSequenceNumber` (because every attempt is front-run), `numHashes` grows with each new user request. Once `numHashes > maxNumHashes`, all new requests revert, halting the Entropy service for that provider.

---

### Impact Explanation

An unprivileged attacker can:
- **Persistently grief the Fortuna keeper**, causing every `advanceProviderCommitment` transaction to fail and wasting keeper gas.
- **Indirectly DoS the Entropy service**: if `maxNumHashes` is set (as it is for the default Fortuna provider via `setMaxNumHashes`), preventing commitment advancement causes `numHashes` to grow until all new randomness requests revert with `LastRevealedTooOld`.
- **Trigger the `sequenceNumber` bump** under specific conditions, permanently skipping sequence numbers and reducing the provider's remaining randomness capacity.

The Entropy service DoS is the highest-impact outcome: users and dApps relying on