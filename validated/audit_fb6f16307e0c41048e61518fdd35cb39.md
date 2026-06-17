### Title
Malicious Requester Contract Can Permanently DoS Provider Keeper via Reverting Callback in Legacy `revealWithCallback` Path — (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.revealWithCallback()`, when a provider has `defaultGasLimit == 0` (the legacy path, where `req.gasLimit10k == 0`), the requester's `_entropyCallback` is invoked **directly with no error catching**. If the requester is a malicious contract that conditionally reverts in its callback, every call to `revealWithCallback` for that request will revert entirely, permanently preventing the provider's keeper from fulfilling the request and causing the keeper to waste gas on every retry attempt.

---

### Finding Description

`revealWithCallback` has two execution branches depending on whether the request has a gas limit set:

**Branch 1 (new flow, `req.gasLimit10k != 0`):** Uses `excessivelySafeCall` to catch reverts, moves the request to `CALLBACK_FAILED` state, and emits a `CallbackFailed` event. The request can be retried.

**Branch 2 (legacy flow, `req.gasLimit10k == 0`):** Calls `_entropyCallback` directly with no error catching. If the callback reverts, the entire transaction reverts — including the preceding `clearRequest` call. [1](#0-0) 

The legacy branch is entered when `providerInfo.defaultGasLimit == 0`: [2](#0-1) 

In the legacy branch, the request is cleared **before** the callback, but since the callback revert bubbles up and reverts the whole transaction, `clearRequest` is also rolled back: [3](#0-2) 

The existing test `testRequestWithCallbackAndRevealWithCallbackFailing` explicitly confirms this behavior — when the requester's callback reverts in the legacy path, `revealWithCallback` reverts entirely and the request remains active: [4](#0-3) 

**Attack scenario:**

1. Attacker deploys a malicious contract with a `_entropyCallback` that reverts when a `revertEnabled` flag is set.
2. Attacker calls `requestWithCallback` against a legacy provider (one with `defaultGasLimit == 0`), paying the required fee.
3. Attacker sets `revertEnabled = true`.
4. Provider's keeper calls `revealWithCallback` → the callback reverts → entire transaction reverts → request stays active.
5. Keeper retries indefinitely, wasting gas on every attempt.
6. Attacker can release the DoS at any time by setting `revertEnabled = false`, e.g., after extracting a side-payment from the provider.

The provider already accrued their fee at request time: [5](#0-4) 

So the provider has their fee but their keeper wastes gas on every failed fulfillment attempt. There is no `cancelRequest` or refund mechanism to clear stuck requests.

---

### Impact Explanation

- The provider's keeper wastes gas indefinitely on stuck requests. If the keeper retries many times, the cumulative gas cost can exceed the fee the provider received.
- The request is permanently stuck in storage with no on-chain recovery path (unlike the new flow which has `CALLBACK_FAILED` + retry).
- The attacker can hold the DoS conditionally and release it at will, creating a ransom-like dynamic analogous to the reNFT M-14 finding.
- The attacker's cost is the fee paid per request; the provider's cost is unbounded gas on retries.

---

### Likelihood Explanation

- Any unprivileged user can deploy a malicious requester contract and call `requestWithCallback`.
- Legacy providers (those who have not called `setDefaultGasLimit` with a non-zero value) are vulnerable. The code explicitly supports `defaultGasLimit == 0` as a valid opt-out state.
- The new callback failure flow (V2) is not retroactive — existing legacy providers remain exposed.
- The test suite confirms the revert propagation behavior exists and is observable. [6](#0-5) 

---

### Recommendation

1. **Wrap the legacy callback in a try/catch** (or use `excessivelySafeCall`) so that a reverting callback does not roll back `clearRequest`. Emit a failure event and clear the request regardless of callback success.
2. **Deprecate the legacy path entirely** by requiring all providers to set a non-zero `defaultGasLimit` before accepting new requests.
3. **Add a `cancelRequest` function** that allows clearing stuck requests after a timeout, so storage is not permanently consumed.

---

### Proof of Concept

```solidity
contract MaliciousRequester is IEntropyConsumer {
    bool public revertEnabled;
    address immutable entropy;

    constructor(address _entropy) { entropy = _entropy; }

    function setRevertEnabled(bool v) external { revertEnabled = v; }

    function getEntropy() internal view override returns (address) { return entropy; }

    function entropyCallback(uint64, address, bytes32) internal override {
        require(!revertEnabled, "griefing");
    }

    function request(address provider, bytes32 userRandom) external payable returns (uint64) {
        return IEntropy(entropy).requestWithCallback{value: msg.value}(provider, userRandom);
    }
}

// 1. Deploy MaliciousRequester against a legacy provider (defaultGasLimit == 0)
// 2. Call request() paying the fee
// 3. Call setRevertEnabled(true)
// 4. Provider keeper calls revealWithCallback() → reverts every time
// 5. Keeper wastes gas indefinitely; request never clears
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L236-239)
```text
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L268-271)
```text
        if (providerInfo.defaultGasLimit == 0) {
            // Provider doesn't support the new callback failure state flow (toggled by setting the gas limit field).
            // Set gasLimit10k to 0 to disable.
            req.gasLimit10k = 0;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-576)
```text
        if (
            req.gasLimit10k != 0 &&
            req.callbackStatus == EntropyStatusConstants.CALLBACK_NOT_STARTED
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

**File:** target_chains/ethereum/contracts/test/Entropy.t.sol (L999-1017)
```text
    function testRequestWithCallbackAndRevealWithCallbackFailing() public {
        bytes32 userRandomNumber = bytes32(uint(42));
        uint fee = random.getFee(provider1);
        EntropyConsumer consumer = new EntropyConsumer(address(random), true);
        vm.deal(address(consumer), fee);
        vm.startPrank(address(consumer));
        uint64 assignedSequenceNumber = random.requestWithCallback{value: fee}(
            provider1,
            userRandomNumber
        );

        vm.expectRevert();
        random.revealWithCallback(
            provider1,
            assignedSequenceNumber,
            userRandomNumber,
            provider1Proofs[assignedSequenceNumber]
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
