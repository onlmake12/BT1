### Title
Unbounded Callback Execution in Legacy `revealWithCallback` Path Enables Gas Griefing and Permanent Request DoS — (`Entropy.sol`)

---

### Summary

In `Entropy.sol`'s `revealWithCallback`, when a request has `gasLimit10k == 0` (set for providers whose `defaultGasLimit == 0`), the requester's `_entropyCallback` is invoked with **no gas cap and no error handling**. A malicious requester can deploy a contract that always reverts or exhausts all gas inside `_entropyCallback`, permanently blocking the provider from fulfilling the request and locking the requester's fee in the contract.

---

### Finding Description

`revealWithCallback` in `Entropy.sol` branches on `req.gasLimit10k`:

**New path** (`gasLimit10k != 0`, lines 574–660): uses `excessivelySafeCall` with an explicit gas cap and catches reverts, moving the request to `CALLBACK_FAILED` state for recovery.

**Legacy path** (`gasLimit10k == 0`, lines 661–702): calls the requester's callback directly with no gas limit and no try/catch:

```solidity
if (len != 0) {
    IEntropyConsumer(callAddress)._entropyCallback(
        sequenceNumber,
        provider,
        randomNumber
    );
}
``` [1](#0-0) 

The legacy path is entered whenever `req.gasLimit10k == 0`. This value is set during `requestHelper` when the provider's `defaultGasLimit == 0`:

```solidity
if (providerInfo.defaultGasLimit == 0) {
    // Provider doesn't support the new callback failure state flow
    req.gasLimit10k = 0;
}
``` [2](#0-1) 

Critically, `clearRequest` is called **before** the callback (CEI pattern), but if the callback reverts, the entire transaction reverts — including `clearRequest`. The request is restored to active state, and the provider can never fulfill it. [3](#0-2) 

The `requestWithCallback` legacy entry point passes `gasLimit = 0`, which flows directly into this path for any provider with `defaultGasLimit == 0`: [4](#0-3) 

---

### Impact Explanation

1. **Gas griefing of the provider**: A malicious requester deploys a contract that burns all forwarded gas inside `_entropyCallback`. The provider's `revealWithCallback` transaction runs out of gas and reverts. The provider wastes gas costs on every fulfillment attempt.
2. **Permanent DoS on the request**: Because the callback revert rolls back `clearRequest`, the request stays active indefinitely. The provider can never fulfill it.
3. **Permanent fund lock**: The requester's fee paid at request time is locked in the Entropy contract with no recovery path.

---

### Likelihood Explanation

Any unprivileged Entropy user can trigger this by:
- Identifying a registered provider whose `defaultGasLimit == 0` (legacy providers that have not called `setDefaultGasLimit`)
- Deploying a malicious `IEntropyConsumer` contract that always reverts or loops in `_entropyCallback`
- Calling `requestWithCallback(provider, userContribution)` from that contract

No privileged access is required. The `requestWithCallback` function is public and payable. [5](#0-4) 

---

### Recommendation

Apply the same `excessivelySafeCall` + gas-cap + error-handling logic to the legacy path, or require all providers to set a non-zero `defaultGasLimit` before accepting callback requests. At minimum, wrap the legacy callback in a try/catch so that a reverting callback does not roll back `clearRequest`, preventing the permanent fund lock.

---

### Proof of Concept

```solidity
// Malicious requester contract
contract MaliciousRequester is IEntropyConsumer {
    address entropy;
    constructor(address _entropy) { entropy = _entropy; }

    function getEntropy() internal view override returns (address) { return entropy; }

    function entropyCallback(uint64, address, bytes32) internal override {
        // Consume all gas or always revert
        revert("griefed");
    }

    function attack(address provider, bytes32 userCommitment) external payable {
        IEntropy(entropy).requestWithCallback{value: msg.value}(provider, userCommitment);
    }
}
```

1. Deploy `MaliciousRequester` pointing at the Entropy contract.
2. Identify a provider with `defaultGasLimit == 0` (legacy provider).
3. Call `attack(legacyProvider, commitment)` with the required fee.
4. Provider calls `revealWithCallback(provider, sequenceNumber, userContribution, providerContribution)`.
5. The callback reverts → entire transaction reverts → `clearRequest` is rolled back → request remains active.
6. Provider can never fulfill this request; requester's fee is permanently locked. [6](#0-5) [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L268-272)
```text
        if (providerInfo.defaultGasLimit == 0) {
            // Provider doesn't support the new callback failure state flow (toggled by setting the gas limit field).
            // Set gasLimit10k to 0 to disable.
            req.gasLimit10k = 0;
        } else {
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L346-356)
```text
    function requestWithCallback(
        address provider,
        bytes32 userContribution
    ) public payable override returns (uint64) {
        return
            requestV2(
                provider,
                userContribution,
                0 // Passing 0 will assign the request the provider's default gas limit
            );
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-660)
```text
        if (
            req.gasLimit10k != 0 &&
            req.callbackStatus == EntropyStatusConstants.CALLBACK_NOT_STARTED
        ) {
            req.callbackStatus = EntropyStatusConstants.CALLBACK_IN_PROGRESS;
            bool success;
            bytes memory ret;
            uint256 startingGas = gasleft();
            (success, ret) = req.requester.excessivelySafeCall(
                // Warning: the provided gas limit below is only an *upper bound* on the gas provided to the call.
                // At most 63/64ths of the current context's gas will be provided to a call, which may be less
                // than the indicated gas limit. (See CALL opcode docs here https://www.evm.codes/?fork=cancun#f1)
                // Consequently, out-of-gas reverts need to be handled carefully to ensure that the callback
                // was truly provided with a sufficient amount of gas.
                uint256(req.gasLimit10k) * TEN_THOUSAND,
                256, // copy at most 256 bytes of the return value into ret.
                abi.encodeWithSelector(
                    IEntropyConsumer._entropyCallback.selector,
                    sequenceNumber,
                    provider,
                    randomNumber
                )
            );
            uint32 gasUsed = SafeCast.toUint32(startingGas - gasleft());
            // Reset status to not started here in case the transaction reverts.
            req.callbackStatus = EntropyStatusConstants.CALLBACK_NOT_STARTED;

            if (success) {
                emit RevealedWithCallback(
                    EntropyStructConverter.toV1Request(req),
                    userContribution,
                    providerContribution,
                    randomNumber
                );
                emit EntropyEventsV2.Revealed(
                    provider,
                    req.requester,
                    req.sequenceNumber,
                    randomNumber,
                    userContribution,
                    providerContribution,
                    false,
                    ret,
                    SafeCast.toUint32(gasUsed),
                    bytes("")
                );
                clearRequest(provider, sequenceNumber);
            } else if (
                (startingGas * 31) / 32 >
                uint256(req.gasLimit10k) * TEN_THOUSAND
            ) {
                // The callback reverted for some reason.
                // We don't use ret to condition the behavior here (out-of-gas or other revert), as we have found that some user contracts
                // catch out-of-gas errors and revert with a different error.
                // In this case, ensure that the callback was provided with sufficient gas. Technically, 63/64ths of the startingGas is forwarded,
                // but we're using 31/32 to introduce a margin of safety.
                emit CallbackFailed(
                    provider,
                    req.requester,
                    sequenceNumber,
                    userContribution,
                    providerContribution,
                    randomNumber,
                    ret
                );
                emit EntropyEventsV2.Revealed(
                    provider,
                    req.requester,
                    sequenceNumber,
                    randomNumber,
                    userContribution,
                    providerContribution,
                    true,
                    ret,
                    SafeCast.toUint32(gasUsed),
                    bytes("")
                );
                req.callbackStatus = EntropyStatusConstants.CALLBACK_FAILED;
            } else {
                // Callback reverted by (potentially) running out of gas, but the calling context did not have enough gas
                // to run the callback. This is a corner case that can happen due to the nuances of gas passing
                // in calls (see the comment on the call above).
                //
                // (Note that reverting here plays nicely with the estimateGas RPC method, which binary searches for
                // the smallest gas value that causes the transaction to *succeed*. See https://github.com/ethereum/go-ethereum/pull/3587 )
                revert EntropyErrors.InsufficientGas();
            }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L661-702)
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
            uint32 gasUsed = SafeCast.toUint32(startingGas - gasleft());

            emit RevealedWithCallback(
                reqV1,
                userContribution,
                providerContribution,
                randomNumber
            );
            emit EntropyEventsV2.Revealed(
                provider,
                callAddress,
                sequenceNumber,
                randomNumber,
                userContribution,
                providerContribution,
                false,
                bytes(""),
                gasUsed,
                bytes("")
            );
        }
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyConsumer.sol (L8-18)
```text
    function _entropyCallback(
        uint64 sequence,
        address provider,
        bytes32 randomNumber
    ) external {
        address entropy = getEntropy();
        require(entropy != address(0), "Entropy address not set");
        require(msg.sender == entropy, "Only Entropy can call this function");

        entropyCallback(sequence, provider, randomNumber);
    }
```
