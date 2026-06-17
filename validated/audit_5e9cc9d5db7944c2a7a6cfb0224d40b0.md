### Title
Fee Loss Due to `LastRevealedTooOld` Revert After Fee Collection in `requestHelper` — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.sol`'s `requestHelper`, the full fee is credited to the provider and Pyth **before** the `LastRevealedTooOld` validity check. When that check reverts, Solidity rolls back the fee credits — but the caller irrecoverably loses the gas paid for the failed transaction. Because the `LastRevealedTooOld` condition is a provider-level "paused" state (the provider has not advanced its commitment and `maxNumHashes` is exceeded), any user who requests randomness from such a provider will have their transaction silently fail after paying gas, with no on-chain or front-end warning.

---

### Finding Description

Inside `requestHelper` the execution order is:

1. **Sequence number assigned** — `providerInfo.sequenceNumber += 1` (line 231)
2. **Fee collected** — `providerInfo.accruedFeesInWei += providerFee` and `_state.accruedPythFeesInWei += …` (lines 237–239)
3. **`LastRevealedTooOld` check** — computed `req.numHashes` is compared against `providerInfo.maxNumHashes` (lines 247–256); if exceeded, `revert EntropyErrors.LastRevealedTooOld()` is thrown

```solidity
// lines 237-239 — fees credited BEFORE the validity check
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);

// lines 247-256 — check that can revert AFTER fees are credited
req.numHashes = SafeCast.toUint32(
    assignedSequenceNumber - providerInfo.currentCommitmentSequenceNumber
);
if (
    providerInfo.maxNumHashes != 0 &&
    req.numHashes > providerInfo.maxNumHashes
) {
    revert EntropyErrors.LastRevealedTooOld();
}
``` [1](#0-0) 

The EVM revert rolls back the storage writes, so the caller's `msg.value` is returned. However, the gas consumed up to the revert point is permanently lost. Because the `LastRevealedTooOld` condition is entirely determined by on-chain state that the caller cannot atomically inspect and act on (TOCTOU), a user who queries the state off-chain and then submits a transaction may find the condition has changed by the time their transaction executes, causing a wasted-gas failure.

The `LastRevealedTooOld` error is defined in `EntropyErrors.sol`:

```solidity
// The last random number revealed from the provider is too old.
error LastRevealedTooOld();
``` [2](#0-1) 

---

### Impact Explanation

Every failed `requestWithCallback` / `requestV2` call due to `LastRevealedTooOld` burns the caller's gas with no service rendered. On high-fee chains (Ethereum mainnet) or during gas-price spikes, this represents a meaningful financial loss per failed attempt. A provider whose keeper lags behind — or who deliberately sets a low `maxNumHashes` — effectively becomes a "paused" endpoint that silently drains callers' gas budgets. There is no on-chain mechanism to warn callers before they submit.

---

### Likelihood Explanation

The condition is reachable by any unprivileged user calling `requestWithCallback` or `requestV2` against a provider whose `maxNumHashes` is non-zero and whose `currentCommitmentSequenceNumber` has not been advanced recently. This is a normal operational state: if a provider's Fortuna keeper is slow, congested, or temporarily offline, the gap between `sequenceNumber` and `currentCommitmentSequenceNumber` grows until it exceeds `maxNumHashes`. At that point every new request reverts. The caller has no atomic way to avoid this; a check-then-act race exists between the off-chain read and the on-chain write. [3](#0-2) 

---

### Recommendation

1. **Move the `LastRevealedTooOld` check before fee collection** so that the revert occurs before any state is mutated, reducing wasted gas and making the failure cheaper.
2. **Expose a view helper** (e.g., `isProviderAcceptingRequests(address provider)`) that front-ends and integrators can call to detect the `LastRevealedTooOld` condition before submitting a transaction.
3. **Notify users on the front-end** when the selected provider is in a `LastRevealedTooOld` state, mirroring the recommendation in the external report.

---

### Proof of Concept

```
1. Provider calls setMaxNumHashes(5).
2. Five requests arrive; provider keeper does not call advanceProviderCommitment.
   → sequenceNumber = 6, currentCommitmentSequenceNumber = 0, numHashes = 6 > 5.
3. User calls requestV2{value: fee}(provider, userRandom, gasLimit).
4. requestHelper:
     a. sequenceNumber check passes (6 < endSequenceNumber).
     b. providerInfo.accruedFeesInWei += providerFee   ← fee credited
     c. _state.accruedPythFeesInWei   += pythFee       ← fee credited
     d. req.numHashes = 6 - 0 = 6 > maxNumHashes(5)
     e. revert LastRevealedTooOld()                    ← all storage rolled back
5. User receives msg.value back but loses all gas paid for the call.
6. Repeat for every subsequent user until the provider advances its commitment.
``` [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L228-256)
```text
        uint64 assignedSequenceNumber = providerInfo.sequenceNumber;
        if (assignedSequenceNumber >= providerInfo.endSequenceNumber)
            revert EntropyErrors.OutOfRandomness();
        providerInfo.sequenceNumber += 1;

        // Check that fees were paid and increment the pyth / provider balances.
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);

        // Store the user's commitment so that we can fulfill the request later.
        // Warning: this code needs to overwrite *every* field in the request, because the returned request can be
        // filled with arbitrary data.
        req = allocRequest(provider, assignedSequenceNumber);
        req.provider = provider;
        req.sequenceNumber = assignedSequenceNumber;
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

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyErrors.sol (L42-43)
```text
    // are required for any new reveal. Please update the currentCommitment before making more requests.
    error LastRevealedTooOld();
```
