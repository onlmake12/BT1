### Title
Permanent DoS on `revealWithCallback` via Reverting Callback in Legacy Path — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.sol`, the `revealWithCallback` function contains a legacy code path (active when `req.gasLimit10k == 0`) that invokes the requester's `_entropyCallback` directly without catching reverts. A malicious requester can deploy a contract whose `_entropyCallback` always reverts, permanently preventing that request from ever being fulfilled and causing every provider attempt to revert — an exact structural analog to the external report's "critical operation coupled with a failable external call."

---

### Finding Description

**Root cause — `requestHelper` (lines 268–283):**

When a provider has `defaultGasLimit == 0` (the default for any provider that has never called `setDefaultGasLimit`), every new request is stored with `req.gasLimit10k = 0`. [1](#0-0) 

**Vulnerable execution path — `revealWithCallback` `else` branch (lines 661–681):**

When `req.gasLimit10k == 0` **and** `callbackStatus == CALLBACK_NOT_STARTED`, the function skips the `excessivelySafeCall` guard and falls into the `else` branch. It:

1. Calls `clearRequest(provider, sequenceNumber)` — a state-changing effect.
2. Then calls `IEntropyConsumer(callAddress)._entropyCallback(...)` **directly**, with no revert-catching wrapper. [2](#0-1) 

If `_entropyCallback` reverts, the **entire transaction reverts**, rolling back `clearRequest`. The request remains permanently active. Every subsequent provider call to `revealWithCallback` for that sequence number will also revert, because the request is never cleared and the callback always reverts.

**Contrast with the new path (lines 574–660):**

When `req.gasLimit10k != 0`, the code uses `excessivelySafeCall`, catches the revert, emits `CallbackFailed`, and transitions the request to `CALLBACK_FAILED` — allowing recovery. The legacy path has no such protection. [3](#0-2) 

---

### Impact Explanation

- **Permanent per-request DoS:** The targeted request can never be cleared or fulfilled. The request slot is permanently occupied (or permanently present in `requestsOverflow`).
- **Provider gas drain:** Every provider attempt to call `revealWithCallback` for the stuck request reverts, wasting gas indefinitely.
- **User fee loss without service:** The user's fee is credited to `providerInfo.accruedFeesInWei` at request time (in `requestHelper`, line 237) and is never refunded. The user paid for randomness they can never receive.
- **Scope match:** Permanent locking of user funds and DoS of the Entropy fulfillment path are within Pyth Immunefi scope. [4](#0-3) 

---

### Likelihood Explanation

- Any provider that has never called `setDefaultGasLimit` has `defaultGasLimit == 0`, making all their requests use the vulnerable legacy path. This is the default state for newly registered providers.
- The attacker only needs to: (a) deploy a contract whose `_entropyCallback` unconditionally reverts, and (b) call `requestWithCallback` paying the provider's fee. No privileged access is required.
- The attack is cheap and repeatable; each stuck request costs only the provider's fee.

---

### Recommendation

Apply `excessivelySafeCall` (already imported and used in the new path) to the legacy `else` branch as well, or unconditionally require `defaultGasLimit != 0` for all providers and remove the legacy path entirely. The new path's `CALLBACK_FAILED` state machine is the correct design.

---

### Proof of Concept

```solidity
// Attacker deploys this contract
contract MaliciousConsumer is IEntropyConsumer {
    IEntropy entropy;
    constructor(address _entropy) { entropy = IEntropy(_entropy); }

    function request(address provider, bytes32 contribution) external payable {
        entropy.requestWithCallback{value: msg.value}(provider, contribution);
    }

    // Always reverts — blocks revealWithCallback permanently
    function _entropyCallback(uint64, address, bytes32) external pure override {
        revert("blocked");
    }
}
```

1. Attacker deploys `MaliciousConsumer`.
2. Attacker calls `request(legacyProvider, randomBytes32)` with the required fee. `legacyProvider` has `defaultGasLimit == 0`, so `req.gasLimit10k = 0`.
3. Provider calls `revealWithCallback(legacyProvider, seqNum, userContrib, providerContrib)`.
4. Execution reaches the `else` branch; `clearRequest` runs, then `_entropyCallback` reverts.
5. Entire transaction reverts; `clearRequest` is rolled back; request remains active.
6. Every future `revealWithCallback` call for this sequence number reverts identically.
7. The user's fee remains in `providerInfo.accruedFeesInWei`; the user never receives randomness. [5](#0-4) [1](#0-0)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L234-239)
```text
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L268-283)
```text
        if (providerInfo.defaultGasLimit == 0) {
            // Provider doesn't support the new callback failure state flow (toggled by setting the gas limit field).
            // Set gasLimit10k to 0 to disable.
            req.gasLimit10k = 0;
        } else {
            // This check does two important things:
            // 1. Providers have a minimum fee set for their defaultGasLimit. If users request less gas than that,
            //    they still pay for the full gas limit. So we may as well give them the full limit here.
            // 2. If a provider has a defaultGasLimit != 0, we need to ensure that all requests have a >0 gas limit
            //    so that we opt-in to the new callback failure state flow.
            req.gasLimit10k = roundTo10kGas(
                callbackGasLimit < providerInfo.defaultGasLimit
                    ? providerInfo.defaultGasLimit
                    : callbackGasLimit
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
