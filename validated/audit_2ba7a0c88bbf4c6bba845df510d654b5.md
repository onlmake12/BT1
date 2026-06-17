### Title
Missing Validation of Zero Commitment in `Entropy.register()` Enables Permanent User Fee Loss — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `Entropy.register()` function accepts a `bytes32 commitment` parameter with no validation that it is non-zero. A malicious actor can permissionlessly register as a provider with `commitment = bytes32(0)`, making it cryptographically impossible to ever fulfill any user request. Because fees are distributed immediately upon request creation, users who request randomness from this provider permanently lose their fees with no refund path.

---

### Finding Description

`Entropy.register()` validates only that `chainLength != 0`, but performs no check that the `commitment` argument is non-zero:

```solidity
function register(
    uint128 feeInWei,
    bytes32 commitment,
    bytes calldata commitmentMetadata,
    uint64 chainLength,
    bytes calldata uri
) public override {
    if (chainLength == 0) revert EntropyErrors.AssertionFailure();
    // No check: commitment != bytes32(0)
    ...
    provider.currentCommitment = commitment;   // stored as bytes32(0)
    ...
}
```

When a user later calls `requestHelper`, the stored `currentCommitment = bytes32(0)` is folded into the request commitment:

```solidity
req.commitment = keccak256(
    bytes.concat(userCommitment, providerInfo.currentCommitment)
);
```

At reveal time, `revealHelper` requires:

```solidity
bytes32 providerCommitment = constructProviderCommitment(
    req.numHashes,
    providerContribution
);
if (keccak256(bytes.concat(userCommitment, providerCommitment)) != req.commitment)
    revert EntropyErrors.IncorrectRevelation();
```

For the check to pass, `providerCommitment` must equal `bytes32(0)`. For the first request `numHashes = 1`, so `constructProviderCommitment(1, x) = keccak256(x)`. Finding `x` such that `keccak256(x) == bytes32(0)` is a preimage attack on keccak256 — computationally infeasible. No valid `providerContribution` can ever satisfy the check, so **every request to this provider is permanently unfulfillable**.

Critically, fees are distributed at request time, before any reveal:

```solidity
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
```

The malicious provider can immediately withdraw their accrued fees via `withdraw()`. There is no `cancelRequest` or refund mechanism in the contract.

---

### Impact Explanation

Users who request randomness from a provider registered with `commitment = bytes32(0)` permanently lose the ETH they paid as fees. Their requests are stored on-chain in a state that can never transition to fulfilled. The malicious provider collects and withdraws the provider-side fees. This is a direct, irreversible loss of user funds with no recovery path.

---

### Likelihood Explanation

The Entropy contract is explicitly designed to be permissionless — anyone can call `register()`. An attacker needs only to:
1. Call `register(lowFee, bytes32(0), ..., largeChainLength, ...)` — one transaction, no special privileges.
2. Advertise the provider address or wait for users to discover it via on-chain events.

The `Registered` event is emitted for every provider, and off-chain tooling (e.g., Fortuna) indexes these events. A provider with a low fee and a large `chainLength` appears indistinguishable from a legitimate provider to users inspecting on-chain state. Likelihood is **medium**.

---

### Recommendation

Add a non-zero check for `commitment` in `register()`:

```solidity
if (commitment == bytes32(0)) revert EntropyErrors.AssertionFailure();
```

This mirrors the existing `chainLength != 0` guard and prevents any provider from registering an unfulfillable commitment.

---

### Proof of Concept

1. Attacker calls:
   ```solidity
   entropy.register(
       1 wei,           // low fee to attract users
       bytes32(0),      // zero commitment — the missing validation
       "",
       1_000_000,       // large chain length
       ""
   );
   ```
   This succeeds. `provider.currentCommitment = bytes32(0)`, `provider.sequenceNumber = 1`.

2. Victim calls:
   ```solidity
   entropy.requestV2{value: fee}(attackerAddress, userContribution, 0);
   ```
   `requestHelper` passes all checks (`sequenceNumber != 0`, `assignedSequenceNumber < endSequenceNumber`, `msg.value >= requiredFee`). Fees are distributed immediately. Victim's ETH is gone.

3. Attacker (or anyone) attempts `revealWithCallback(attackerAddress, 1, userContribution, anyValue)`:
   - `constructProviderCommitment(1, anyValue) = keccak256(anyValue)`
   - `keccak256(anyValue) != bytes32(0)` for all feasible inputs
   - Reverts with `IncorrectRevelation` every time.

4. Attacker calls `withdraw()` and collects their accrued `providerFee`. Victim's request is permanently stuck. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L150-173)
```text
    function withdraw(uint128 amount) public override {
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            msg.sender
        ];

        // Use checks-effects-interactions pattern to prevent reentrancy attacks.
        require(
            providerInfo.accruedFeesInWei >= amount,
            "Insufficient balance"
        );
        providerInfo.accruedFeesInWei -= amount;

        // Interaction with an external contract or token transfer
        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "withdrawal to msg.sender failed");

        emit EntropyEvents.Withdrawal(msg.sender, msg.sender, amount);
        emit EntropyEventsV2.Withdrawal(
            msg.sender,
            msg.sender,
            amount,
            bytes("")
        );
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L221-239)
```text
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            provider
        ];
        if (_state.providers[provider].sequenceNumber == 0)
            revert EntropyErrors.NoSuchProvider();

        // Assign a sequence number to the request
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
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L257-259)
```text
        req.commitment = keccak256(
            bytes.concat(userCommitment, providerInfo.currentCommitment)
        );
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L395-408)
```text
    function revealHelper(
        EntropyStructsV2.Request storage req,
        bytes32 userContribution,
        bytes32 providerContribution
    ) internal returns (bytes32 randomNumber, bytes32 blockHash) {
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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L985-996)
```text
    // Construct a provider's commitment given their revealed random number and the distance in the hash chain
    // between the commitment and the revealed random number.
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
