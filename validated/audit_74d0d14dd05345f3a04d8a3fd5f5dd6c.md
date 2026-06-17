### Title
`advanceProviderCommitment` Lacks Caller Restriction, Allowing Anyone to Advance a Provider's Commitment — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary
`Entropy.advanceProviderCommitment` is declared `public` and accepts an arbitrary `provider` address without verifying that `msg.sender == provider`. Any caller who possesses a valid hash-chain preimage for the target provider can invoke this function on behalf of that provider, advancing their commitment state and — critically — burning unrequested sequence numbers.

---

### Finding Description
`advanceProviderCommitment` is intended to be called by a provider to reduce future `numHashes` gas costs by advancing their on-chain commitment pointer. The function signature is:

```solidity
function advanceProviderCommitment(
    address provider,
    uint64 advancedSequenceNumber,
    bytes32 providerContribution
) public override {
``` [1](#0-0) 

There is no `require(msg.sender == provider, ...)` guard. The only protection is the hash-chain proof check:

```solidity
if (providerCommitment != providerInfo.currentCommitment)
    revert EntropyErrors.IncorrectRevelation();
``` [2](#0-1) 

If `advancedSequenceNumber >= providerInfo.sequenceNumber`, the function silently bumps the provider's `sequenceNumber` forward:

```solidity
providerInfo.sequenceNumber =
    providerInfo.currentCommitmentSequenceNumber + 1;
``` [3](#0-2) 

The inline comment explicitly states this path is reserved for the provider themselves ("This means the **provider** called the function with a sequence number that was not yet requested"), yet no access control enforces this intent. [4](#0-3) 

---

### Impact Explanation
An attacker who obtains a valid `providerContribution` for a future sequence number (e.g., by observing a provider's off-chain keeper service, a leaked key, or a provider that inadvertently publishes future chain values) can:

1. Call `advanceProviderCommitment` with `advancedSequenceNumber >= providerInfo.sequenceNumber`.
2. Force the provider's `sequenceNumber` to jump forward, permanently burning the skipped range of sequence numbers.
3. Exhaust the provider's committed chain length (`endSequenceNumber`) prematurely, triggering `OutOfRandomness` reverts for all future user requests to that provider.

This constitutes a **provider-targeted denial-of-service**: legitimate users can no longer obtain randomness from the affected provider until the provider re-registers with a new commitment chain. [5](#0-4) 

---

### Likelihood Explanation
The attacker must supply a `providerContribution` that satisfies the hash-chain verification against `providerInfo.currentCommitment`. This value is the provider's secret preimage for a future sequence number and is not directly observable on-chain. Likelihood is therefore **low-to-medium**: it requires either a leaked provider secret, a compromised keeper process, or a provider that mistakenly publishes future chain values. However, the function's public surface unnecessarily expands the attack surface beyond what the protocol design requires.

---

### Recommendation
Add an explicit caller check at the top of `advanceProviderCommitment`:

```solidity
require(msg.sender == provider, "Only provider can advance commitment");
```

This mirrors the pattern used in every other provider-mutating function in the contract (e.g., `setProviderFee`, `setProviderUri`, `setFeeManager`, `setMaxNumHashes`, `setDefaultGasLimit`), all of which operate on `_state.providers[msg.sender]` rather than accepting an arbitrary `provider` address. [6](#0-5) 

---

### Proof of Concept

1. Provider P registers with a hash chain of length N, committing to `x_0`. Their `sequenceNumber` starts at 1.
2. Several users request randomness; P fulfills them via `revealWithCallback`, advancing `currentCommitmentSequenceNumber` to, say, 50. The revealed value `x_50` is now public in transaction calldata/events.
3. Attacker observes `x_50` and, knowing the hash chain structure, computes `x_51` (if P's keeper software leaks it or if P's RNG seed is compromised).
4. Attacker calls:
   ```solidity
   entropy.advanceProviderCommitment(
       address(P),
       providerInfo.sequenceNumber, // e.g., 51, which equals current sequenceNumber
       x_51
   );
   ```
5. The hash-chain check passes. Because `advancedSequenceNumber (51) >= sequenceNumber (51)`, the contract sets `providerInfo.sequenceNumber = 52`, burning sequence number 51.
6. Repeated calls with successive preimages exhaust the chain, causing all future `requestV2` calls to revert with `OutOfRandomness`. [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L443-484)
```text
    function advanceProviderCommitment(
        address provider,
        uint64 advancedSequenceNumber,
        bytes32 providerContribution
    ) public override {
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            provider
        ];
        if (
            advancedSequenceNumber <=
            providerInfo.currentCommitmentSequenceNumber
        ) revert EntropyErrors.UpdateTooOld();
        if (advancedSequenceNumber >= providerInfo.endSequenceNumber)
            revert EntropyErrors.AssertionFailure();

        uint32 numHashes = SafeCast.toUint32(
            advancedSequenceNumber -
                providerInfo.currentCommitmentSequenceNumber
        );
        bytes32 providerCommitment = constructProviderCommitment(
            numHashes,
            providerContribution
        );

        if (providerCommitment != providerInfo.currentCommitment)
            revert EntropyErrors.IncorrectRevelation();

        providerInfo.currentCommitmentSequenceNumber = advancedSequenceNumber;
        providerInfo.currentCommitment = providerContribution;
        if (
            providerInfo.currentCommitmentSequenceNumber >=
            providerInfo.sequenceNumber
        ) {
            // This means the provider called the function with a sequence number that was not yet requested.
            // Providers should never do this and we consider such an implementation flawed.
            // Assuming this is landed on-chain it's better to bump the sequence number and never use that range
            // for future requests. Otherwise, someone can use the leaked revelation to derive favorable random numbers.
            providerInfo.sequenceNumber =
                providerInfo.currentCommitmentSequenceNumber +
                1;
        }
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L810-820)
```text
    function setProviderFee(uint128 newFeeInWei) external override {
        EntropyStructsV2.ProviderInfo storage provider = _state.providers[
            msg.sender
        ];

        if (provider.sequenceNumber == 0) {
            revert EntropyErrors.NoSuchProvider();
        }
        uint128 oldFeeInWei = provider.feeInWei;
        provider.feeInWei = newFeeInWei;
        emit ProviderFeeUpdated(msg.sender, oldFeeInWei, newFeeInWei);
```
