### Title
Malicious Entropy Requester Can Permanently DoS Provider Fulfillment via Reverting Callback in Legacy `revealWithCallback` Path — (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.sol`, the `revealWithCallback` function has two execution paths depending on whether `req.gasLimit10k` is zero. When a provider has `defaultGasLimit == 0` (the legacy configuration), the callback to the requester contract is made **without** a try/catch wrapper. A malicious requester can deploy a contract whose `_entropyCallback` always reverts, causing the entire `revealWithCallback` transaction to revert. Because `clearRequest` is called before the callback but is undone by the revert, the request remains permanently active with no recovery path, and the provider wastes gas on every fulfillment attempt indefinitely.

---

### Finding Description

`revealWithCallback` branches on `req.gasLimit10k`:

**New path** (`gasLimit10k != 0`): uses `excessivelySafeCall` — callback failures are caught, emitted as `CallbackFailed`, and the request moves to `CALLBACK_FAILED` state for recovery.

**Legacy path** (`gasLimit10k == 0`): calls `_entropyCallback` directly with no error handling:

```solidity
clearRequest(provider, sequenceNumber);
// WARNING: DO NOT USE req BELOW HERE AS ITS CONTENTS HAS BEEN CLEARED

if (len != 0) {
    IEntropyConsumer(callAddress)._entropyCallback(
        sequenceNumber,
        provider,
        randomNumber
    );
}
``` [1](#0-0) 

If `_entropyCallback` reverts, the entire transaction reverts — including the `clearRequest` call — leaving the request active. There is no `CALLBACK_FAILED` state in this path, so there is no recovery mechanism.

The legacy path is entered when `providerInfo.defaultGasLimit == 0`:

```solidity
if (providerInfo.defaultGasLimit == 0) {
    // Provider doesn't support the new callback failure state flow
    req.gasLimit10k = 0;
}
``` [2](#0-1) 

`requestWithCallback` explicitly passes `gasLimit = 0`, which triggers this branch for any provider with `defaultGasLimit == 0`:

```solidity
function requestWithCallback(address provider, bytes32 userContribution)
    public payable override returns (uint64) {
    return requestV2(provider, userContribution, 0);
}
``` [3](#0-2) 

The new path correctly handles this with `excessivelySafeCall` and a `CALLBACK_FAILED` state, but the legacy path has no equivalent protection: [4](#0-3) 

The test suite explicitly acknowledges this behavioral difference:

```solidity
// A provider that hasn't upgraded to the callback failure flow
// can never cause a callback to fail because it runs out of gas.
vm.prank(provider1);
random.setDefaultGasLimit(0);
``` [5](#0-4) 

---

### Impact Explanation

- The provider can **never successfully fulfill** the request — every `revealWithCallback` call reverts.
- The request occupies a storage slot indefinitely; there is no expiry or cancellation function.
- The provider wastes gas on every fulfillment attempt with no recourse.
- The requester's random number is permanently undeliverable.
- Unlike the new path, there is no `CALLBACK_FAILED` state and no retry/recovery flow.

---

### Likelihood Explanation

- Any unprivileged user can deploy a contract that reverts in `_entropyCallback` and call `requestWithCallback` against any provider with `defaultGasLimit == 0`.
- The attacker pays the request fee (already credited to the provider at request time via `requestHelper`), so the cost is the fee plus deployment gas — a low barrier.
- Legacy providers (those that have not called `setDefaultGasLimit`) are permanently vulnerable to this pattern.
- The attacker loses their fee and their random number, but the provider suffers unbounded gas waste and a permanently stuck request. [6](#0-5) 

---

### Recommendation

Wrap the legacy-path callback in a try/catch (or `excessivelySafeCall`) identical to the new path, and introduce a `CALLBACK_FAILED` state for legacy requests so providers can mark them as unrecoverable and move on. Alternatively, deprecate the legacy path entirely by requiring all providers to set a non-zero `defaultGasLimit` before accepting new requests.

---

### Proof of Concept

1. Deploy a malicious consumer contract:
```solidity
contract MaliciousConsumer is IEntropyConsumer {
    address entropy;
    constructor(address _entropy) { entropy = _entropy; }
    function getEntropy() internal view override returns (address) { return entropy; }
    function entropyCallback(uint64, address, bytes32) internal override {
        revert("always revert");
    }
}
```

2. Target a provider with `defaultGasLimit == 0` (legacy provider). Call `requestWithCallback`:
```solidity
uint64 seq = maliciousConsumer.requestWithCallback{value: fee}(legacyProvider, userRandom);
```

3. Provider calls `revealWithCallback(legacyProvider, seq, userContrib, providerContrib)` → always reverts.

4. `getRequest(legacyProvider, seq)` still returns an active request. The provider has no mechanism to clear it or move to a failed state. Every subsequent fulfillment attempt reverts, wasting gas indefinitely. [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L236-239)
```text
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);
```

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-599)
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

**File:** target_chains/ethereum/contracts/test/Entropy.t.sol (L1801-1808)
```text
        // A provider that hasn't upgraded to the callback failure flow
        // can never cause a callback to fail because it runs out of gas.
        vm.prank(provider1);
        random.setDefaultGasLimit(0);
        assertCallbackResult(0, 190000, true);
        assertCallbackResult(0, 210000, true);
        assertCallbackResult(300000, 290000, true);
        assertCallbackResult(300000, 310000, true);
```
