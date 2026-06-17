### Title
Entropy Request Permanently Stuck When `requestWithCallback` Is Called During Contract Constructor — (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.revealWithCallback`, the legacy fulfillment path (used when `req.gasLimit10k == 0`) uses `extcodesize` to decide whether to invoke `_entropyCallback` on the requester. A contract that calls `requestWithCallback` during its own constructor has `extcodesize == 0` at that moment, so no callback compatibility is enforced at request time. After the constructor completes and the contract is fully deployed, `extcodesize` returns non-zero. When `revealWithCallback` is then called, the Entropy contract attempts to invoke `IEntropyConsumer._entropyCallback` on the requester. If the requester does not implement `IEntropyConsumer` (or its callback reverts), the entire `revealWithCallback` call reverts — including the preceding `clearRequest` — leaving the request permanently active and unfulfillable.

---

### Finding Description

`Entropy.revealWithCallback` contains two fulfillment paths. The new path (when `req.gasLimit10k != 0`) uses `excessivelySafeCall` to catch callback reverts and transitions the request to `CALLBACK_FAILED`, from which recovery is possible. The legacy path (when `req.gasLimit10k == 0`, triggered when the provider has `defaultGasLimit == 0`) does not use a safe call: [1](#0-0) 

In this legacy path, `clearRequest` is called first (checks-effects-interactions), then `extcodesize` is evaluated on the stored `req.requester`. If the requester has code, `_entropyCallback` is called directly with no revert protection. If the callback reverts, the entire transaction reverts — including `clearRequest` — so the request remains active.

The root cause is the inconsistency between request time and reveal time:

- At **request time** (`requestHelper`), `req.requester = msg.sender` is stored with no check on whether `msg.sender` implements `IEntropyConsumer`: [2](#0-1) 

- At **reveal time**, `extcodesize` is used to decide whether to invoke the callback: [3](#0-2) 

When a contract calls `requestWithCallback` during its own constructor, `extcodesize(address(this)) == 0` at that moment (EVM rule: a contract has no code until its constructor returns). The request is accepted and stored. After deployment, `extcodesize` returns non-zero. `revealWithCallback` then attempts the callback, which reverts if the contract does not implement `IEntropyConsumer`. Because there is no revert protection in the legacy path, the request is permanently stuck.

The fee paid at request time is already distributed to the provider and Pyth at `requestHelper` execution: [4](#0-3) 

The fee cannot be recovered. The request occupies a slot in the fixed-size request ring buffer and consumes a sequence number from the provider's chain, but can never be cleared.

---

### Impact Explanation

- The in-flight request is permanently unfulfillable: every call to `revealWithCallback` reverts.
- The fee paid by the user is irrecoverably distributed to the provider and Pyth.
- The provider's sequence number is consumed, reducing available randomness.
- The request slot in the ring buffer remains occupied until overwritten by a future request at the same slot index, at which point the old request is silently overwritten (potential secondary issue).

This matches the "soft-stuck funds" class from the reference report: assets (fees) are lost and the protocol state (request) is permanently blocked for the affected user.

---

### Likelihood Explanation

The scenario requires a contract to call `requestWithCallback` during its constructor. This is a realistic pattern for:

- Factory contracts that deploy child contracts which initialize randomness requests in their constructors.
- Contracts that use a single-transaction deploy-and-request pattern for gas efficiency.
- Contracts that inherit from a base that calls `requestWithCallback` in an `initialize`-style function invoked from the constructor.

The provider must also have `defaultGasLimit == 0` (legacy path). Providers that have not explicitly set a default gas limit fall into this category, as `defaultGasLimit` defaults to zero: [5](#0-4) 

---

### Recommendation

1. **At request time**, validate that `msg.sender` either has no code (EOA) or implements `IEntropyConsumer`. This mirrors the ERC1155 `_doSafeTransferAcceptanceCheck` pattern and prevents incompatible contracts from creating unfulfillable requests.

2. **In the legacy fulfillment path**, wrap the `_entropyCallback` call in a `try/catch` or use `excessivelySafeCall` (already imported) to prevent a reverting callback from permanently blocking the request. Transition to a `CALLBACK_FAILED` state analogous to the new path.

3. **Document** that contracts calling `requestWithCallback` from within a constructor will have `extcodesize == 0` at request time but non-zero at reveal time, and must implement `IEntropyConsumer` before the reveal transaction is submitted.

---

### Proof of Concept

```solidity
// SPDX-License-Identifier: Apache 2
pragma solidity ^0.8.0;

// Does NOT implement IEntropyConsumer
contract VulnerableRequester {
    uint64 public seqNum;

    constructor(address entropy, address provider, bytes32 userRandom) payable {
        // Called during constructor: extcodesize(address(this)) == 0 here
        // Entropy stores msg.sender (this address) as req.requester
        // No IEntropyConsumer check is performed at request time
        uint128 fee = IEntropy(entropy).getFee(provider);
        seqNum = IEntropy(entropy).requestWithCallback{value: fee}(
            provider,
            userRandom
        );
        // Constructor returns → contract is now deployed, extcodesize > 0
    }
    // No _entropyCallback / IEntropyConsumer implementation
}

// Attack flow:
// 1. Deploy VulnerableRequester with sufficient ETH → request stored, fee paid
// 2. Provider calls revealWithCallback(provider, seqNum, userRandom, providerProof)
//    → extcodesize(VulnerableRequester) != 0
//    → IEntropyConsumer(VulnerableRequester)._entropyCallback(...) called
//    → reverts (no implementation)
//    → clearRequest is reverted too
//    → request permanently stuck, fee irrecoverable
```

The `extcodesize` check at reveal time: [3](#0-2) 

The absence of any `IEntropyConsumer` check at request time: [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L236-239)
```text
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L260-260)
```text
        req.requester = msg.sender;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L268-272)
```text
        if (providerInfo.defaultGasLimit == 0) {
            // Provider doesn't support the new callback failure state flow (toggled by setting the gas limit field).
            // Set gasLimit10k to 0 to disable.
            req.gasLimit10k = 0;
        } else {
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L358-390)
```text
    function requestV2(
        address provider,
        bytes32 userContribution,
        uint32 gasLimit
    ) public payable override returns (uint64) {
        EntropyStructsV2.Request storage req = requestHelper(
            provider,
            constructUserCommitment(userContribution),
            // If useBlockHash is set to true, it allows a scenario in which the provider and miner can collude.
            // If we remove the blockHash from this, the provider would have no choice but to provide its committed
            // random number. Hence, useBlockHash is set to false.
            false,
            true,
            gasLimit
        );

        emit RequestedWithCallback(
            provider,
            req.requester,
            req.sequenceNumber,
            userContribution,
            EntropyStructConverter.toV1Request(req)
        );
        emit EntropyEventsV2.Requested(
            provider,
            req.requester,
            req.sequenceNumber,
            userContribution,
            uint32(req.gasLimit10k) * TEN_THOUSAND,
            bytes("")
        );
        return req.sequenceNumber;
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L661-681)
```text
        } else {
            // This case uses the checks-effects-interactions pattern to avoid reentry attacks
            address callAddress = req.requester;
            EntropyStructs.Request memory reqV1 = EntropyStructConverter
                .toV1Request(req);
            clearRequest(provider, sequenceNumber);
            // WARNING: DO NOT USE req BELOW HERE AS ITS CONTENTS HAS BEEN CLEARED

            // Check if the requester is a contract account.
            uint len;
            assembly {
                len := extcodesize(callAddress)
            }
            uint256 startingGas = gasleft();
            if (len != 0) {
                IEntropyConsumer(callAddress)._entropyCallback(
                    sequenceNumber,
                    provider,
                    randomNumber
                );
            }
```
