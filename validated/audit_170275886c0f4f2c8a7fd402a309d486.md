### Title
Malicious Entropy Requester Can Permanently Grief Keeper via Reverting Callback in Legacy `revealWithCallback` Path — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In the legacy execution branch of `revealWithCallback`, when a provider's `defaultGasLimit` is zero, the Entropy contract calls `_entropyCallback` on the requester's contract **without any revert protection**. A malicious requester can deploy a contract whose `_entropyCallback` always reverts, causing every keeper attempt to fulfill the request to revert and waste gas, while the request remains permanently stuck in storage.

---

### Finding Description

`revealWithCallback` in `Entropy.sol` has two distinct execution paths, selected at lines 574–576:

```solidity
if (
    req.gasLimit10k != 0 &&
    req.callbackStatus == EntropyStatusConstants.CALLBACK_NOT_STARTED
) {
    // NEW PATH: uses excessivelySafeCall — reverts are caught
} else {
    // LEGACY PATH: direct call — reverts propagate to caller
}
```

The **new path** (lines 578–660) uses `excessivelySafeCall`, which catches any revert from the consumer contract and records a `CALLBACK_FAILED` state, allowing the keeper to succeed regardless of consumer behavior.

The **legacy path** (lines 661–702) is entered when `req.gasLimit10k == 0`. This happens when the provider registered with `defaultGasLimit == 0` (lines 268–271 in `requestHelper`):

```solidity
if (providerInfo.defaultGasLimit == 0) {
    req.gasLimit10k = 0;
}
```

In the legacy path, the callback is invoked directly with no error handling:

```solidity
if (len != 0) {
    IEntropyConsumer(callAddress)._entropyCallback(
        sequenceNumber,
        provider,
        randomNumber
    );
}
```

`clearRequest` is called **before** the callback (line 666), but because the callback revert propagates up and reverts the entire transaction, all state changes — including `clearRequest` — are rolled back. The request remains active in storage indefinitely.

**Attack path:**
1. Attacker deploys `MaliciousConsumer` implementing `IEntropyConsumer` with `_entropyCallback` that unconditionally reverts.
2. Attacker calls `requestWithCallback(legacyProvider, userContribution)` where `legacyProvider.defaultGasLimit == 0`. The fee is paid; the request is stored with `gasLimit10k = 0`.
3. The keeper calls `revealWithCallback(provider, sequenceNumber, userContribution, providerContribution)`.
4. Execution enters the legacy path; `_entropyCallback` on `MaliciousConsumer` reverts.
5. The entire transaction reverts. The keeper burns gas and earns nothing. The request is never cleared.
6. Steps 3–5 repeat indefinitely — the keeper can never fulfill this request.

---

### Impact Explanation

- **Keeper DoS**: Every `revealWithCallback` attempt for the malicious request reverts, wasting keeper gas with no reward.
- **Permanent request lock**: Because the transaction reverts, `clearRequest` is rolled back. The slot is occupied forever, and the provider's sequence number is consumed.
- **Scalable grief**: The attacker pays only the request fee per slot. They can open many such requests against any legacy provider (one with `defaultGasLimit == 0`), multiplying the keeper's wasted gas.
- **Provider reputation damage**: Unfulfillable requests accumulate, degrading the provider's service metrics.

---

### Likelihood Explanation

- Any unprivileged user can call `requestWithCallback` — no special role required.
- Any provider that registered before `defaultGasLimit` was introduced, or that explicitly registered with `defaultGasLimit == 0`, is vulnerable.
- Deploying a reverting callback contract costs only a small amount of gas and is trivially achievable.
- The attack is deterministic and repeatable with no probabilistic element.

---

### Recommendation

Apply the same `excessivelySafeCall` pattern used in the new path to the legacy path as well. Replace the bare call at lines 675–681:

```solidity
// BEFORE (legacy path — vulnerable)
if (len != 0) {
    IEntropyConsumer(callAddress)._entropyCallback(
        sequenceNumber,
        provider,
        randomNumber
    );
}
```

with a safe call that catches reverts:

```solidity
// AFTER
if (len != 0) {
    (bool success, ) = callAddress.excessivelySafeCall(
        gasleft(),
        256,
        abi.encodeWithSelector(
            IEntropyConsumer._entropyCallback.selector,
            sequenceNumber,
            provider,
            randomNumber
        )
    );
    if (!success) {
        emit CallbackFailed(provider, callAddress, sequenceNumber, ...);
    }
}
```

Alternatively, migrate all providers to set a non-zero `defaultGasLimit` and deprecate the legacy path entirely.

---

### Proof of Concept

```solidity
// MaliciousConsumer.sol
contract MaliciousConsumer is IEntropyConsumer {
    address immutable entropy;
    constructor(address _entropy) { entropy = _entropy; }

    function getEntropy() internal view override returns (address) {
        return entropy;
    }

    // Always reverts — griefs any keeper that calls revealWithCallback
    function entropyCallback(
        uint64, address, bytes32
    ) internal override {
        revert("grief");
    }

    function requestRandom(address provider, bytes32 userNum)
        external payable returns (uint64)
    {
        return IEntropy(entropy).requestWithCallback{value: msg.value}(
            provider, userNum
        );
    }
}
```

**Steps:**
1. Identify a provider with `defaultGasLimit == 0` (legacy provider).
2. Deploy `MaliciousConsumer`.
3. Call `requestRandom(legacyProvider, someBytes32)` with the required fee.
4. Observe that every subsequent `revealWithCallback` call by the keeper reverts, confirmed by the transaction receipt showing revert with reason `"grief"`.
5. The request slot remains occupied; the keeper's gas is wasted on every attempt.

**Root cause lines:** [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

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
