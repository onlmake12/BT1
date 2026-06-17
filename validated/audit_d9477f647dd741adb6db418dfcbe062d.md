### Title
Entropy Provider Can Register With Zero Commitment, Permanently Trapping User Fees — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

### Summary
`Entropy.register()` accepts `commitment = bytes32(0)` without validation. Any unprivileged address can register as a provider with a zero commitment, collect fees from users who request randomness, and never be able to fulfill those requests. User fees are permanently trapped in the contract with no recovery path.

### Finding Description
The `register()` function in `Entropy.sol` enforces only one input constraint: `chainLength != 0`. It does not validate that `commitment != bytes32(0)`. [1](#0-0) 

When a provider registers with `commitment = bytes32(0)`:
- `provider.originalCommitment = bytes32(0)`
- `provider.currentCommitment = bytes32(0)` [2](#0-1) 

Users who subsequently request randomness from this provider have their request commitment stored as:

```
req.commitment = keccak256(userCommitment || bytes32(0))
``` [3](#0-2) 

At reveal time, `revealHelper` requires `constructProviderCommitment(numHashes, providerContribution) == currentCommitment == bytes32(0)`. The `constructProviderCommitment` function computes `keccak256^numHashes(providerContribution)`: [4](#0-3) 

Since `numHashes >= 1` for every request (it equals `assignedSequenceNumber - currentCommitmentSequenceNumber`, and `sequenceNumber` is incremented to 1 during registration while `currentCommitmentSequenceNumber` stays at 0), the provider must supply a `providerContribution` such that `keccak256(providerContribution) = bytes32(0)`. This requires finding a keccak256 preimage of zero — computationally infeasible. The reveal check therefore always reverts with `IncorrectRevelation`: [5](#0-4) 

### Impact Explanation
A malicious actor registers with `commitment = bytes32(0)` and a non-zero `feeInWei`. Users who request randomness from this provider pay fees (split between the provider's `accruedFeesInWei` and the protocol's `accruedPythFeesInWei`), but their requests can never be fulfilled. The provider can then call `withdraw()` to extract the accrued fees. Users have no recourse — there is no cancellation or refund mechanism for stuck requests. Every fee paid to this provider is permanently lost to the user.

**Impact: 3/5** — Direct, irreversible loss of user funds (fees paid for randomness that can never be delivered).

### Likelihood Explanation
The Entropy protocol is explicitly permissionless — any address can call `register()`. A malicious provider can advertise a low fee to attract users, register with `commitment = bytes32(0)`, and drain fees from any user who requests from them. No privileged access, leaked key, or external oracle behavior is required. The only friction is that users must choose this provider, but off-chain advertising or front-end manipulation can direct users to it.

**Likelihood: 3/5** — Requires users to select the malicious provider, but the permissionless registration model and absence of any on-chain commitment validity check make this straightforwardly exploitable.

### Recommendation
Add a non-zero check on `commitment` in `register()`:

```solidity
function register(...) public override {
    if (chainLength == 0) revert EntropyErrors.AssertionFailure();
    if (commitment == bytes32(0)) revert EntropyErrors.AssertionFailure(); // ADD THIS
    ...
}
```

This directly mirrors the fix applied in the referenced appchain report, which enforced non-empty values for all fields critical to the registration's intended functionality.

### Proof of Concept
1. Attacker calls `register(feeInWei=1, commitment=bytes32(0), commitmentMetadata="", chainLength=1000, uri="")`.
2. Contract stores `currentCommitment = bytes32(0)`, `sequenceNumber = 1`, `endSequenceNumber = 1000`.
3. User calls `request{value: 1}(attacker, userCommitment, false)`. Fee is accepted; `req.commitment = keccak256(userCommitment || bytes32(0))` is stored.
4. Attacker (or anyone) attempts `reveal(attacker, 1, userRandomness, providerContribution)`. `constructProviderCommitment(1, providerContribution) = keccak256(providerContribution)`. This can never equal `bytes32(0)`, so the call always reverts with `IncorrectRevelation`.
5. Attacker calls `withdraw(1)` and recovers the fee. User's request is permanently stuck with no refund path.

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L111-145)
```text
    function register(
        uint128 feeInWei,
        bytes32 commitment,
        bytes calldata commitmentMetadata,
        uint64 chainLength,
        bytes calldata uri
    ) public override {
        if (chainLength == 0) revert EntropyErrors.AssertionFailure();

        EntropyStructsV2.ProviderInfo storage provider = _state.providers[
            msg.sender
        ];

        // NOTE: this method implementation depends on the fact that ProviderInfo will be initialized to all-zero.
        // Specifically, accruedFeesInWei is intentionally not set. On initial registration, it will be zero,
        // then on future registrations, it will be unchanged. Similarly, provider.sequenceNumber defaults to 0
        // on initial registration.

        provider.feeInWei = feeInWei;

        provider.originalCommitment = commitment;
        provider.originalCommitmentSequenceNumber = provider.sequenceNumber;
        provider.currentCommitment = commitment;
        provider.currentCommitmentSequenceNumber = provider.sequenceNumber;
        provider.commitmentMetadata = commitmentMetadata;
        provider.endSequenceNumber = provider.sequenceNumber + chainLength;
        provider.uri = uri;

        provider.sequenceNumber += 1;

        emit EntropyEvents.Registered(
            EntropyStructConverter.toV1ProviderInfo(provider)
        );
        emit EntropyEventsV2.Registered(msg.sender, bytes(""));
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L257-259)
```text
        req.commitment = keccak256(
            bytes.concat(userCommitment, providerInfo.currentCommitment)
        );
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L400-408)
```text
        bytes32 providerCommitment = constructProviderCommitment(
            req.numHashes,
            providerContribution
        );
        bytes32 userCommitment = constructUserCommitment(userContribution);
        if (
            keccak256(bytes.concat(userCommitment, providerCommitment)) !=
            req.commitment
        ) revert EntropyErrors.IncorrectRevelation();
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L987-996)
```text
    function constructProviderCommitment(
        uint64 numHashes,
        bytes32 revelation
    ) internal pure returns (bytes32 currentHash) {
        currentHash = revelation;
        while (numHashes > 0) {
            currentHash = keccak256(bytes.concat(currentHash));
            numHashes -= 1;
        }
    }
```
