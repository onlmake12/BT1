### Title
Malicious Requester Contract Can Selectively Reject Random Numbers via Reverting Callback in Legacy `revealWithCallback` Path — (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.sol`, the `revealWithCallback` function has two execution paths depending on whether `req.gasLimit10k` is zero or non-zero. When `gasLimit10k == 0` (the legacy path, triggered when the provider's `defaultGasLimit` is 0), the callback to the requester's contract is made with a bare, unchecked external call — no `try/catch` and no `excessivelySafeCall`. If the requester's `_entropyCallback` reverts, the entire `revealWithCallback` transaction reverts, including the preceding `clearRequest` call. The request remains permanently in-flight. Because the random number is computed and passed to the callback *before* the revert, a malicious requester contract can inspect the random number and selectively revert to reject unfavorable outcomes, then re-request until a favorable number is obtained.

---

### Finding Description

`revealWithCallback` branches on `req.gasLimit10k`: [1](#0-0) 

When `gasLimit10k != 0` (new path), the callback is wrapped in `excessivelySafeCall`, which catches reverts and transitions the request to `CALLBACK_FAILED` state — the request is still cleared and the failure is recoverable. [2](#0-1) 

When `gasLimit10k == 0` (old/legacy path), the callback is a bare, unchecked call: [3](#0-2) 

`clearRequest` is called *before* the callback (checks-effects-interactions pattern), but because the callback revert propagates upward and reverts the entire transaction, `clearRequest` is also undone. The request remains active.

`gasLimit10k` is set to `0` in `requestHelper` whenever the provider's `defaultGasLimit` is `0`: [4](#0-3) 

The random number is fully computed in `revealHelper` and passed as an argument to the callback: [5](#0-4) [6](#0-5) 

This means the requester's callback receives the final `randomNumber` value and can branch on it before deciding whether to revert.

---

### Impact Explanation

**Randomness manipulation (selective reveal):** A malicious requester contract can implement `_entropyCallback` to inspect the `randomNumber` argument and revert if the value is unfavorable (e.g., does not satisfy a winning condition). The revert rolls back `clearRequest`, leaving the request in-flight. The attacker then submits a new request (paying the fee again) to obtain a different random number. By repeating this, the attacker can keep sampling until a favorable outcome is obtained. For any application using Entropy for high-value outcomes (lotteries, NFT trait rolls, on-chain games), this completely breaks the randomness guarantee.

**Permanent keeper DoS:** The provider's off-chain keeper (Fortuna) will repeatedly attempt to fulfill the stuck request, wasting gas indefinitely. A batch of such malicious requests can exhaust the keeper's gas budget and delay fulfillment of legitimate requests.

---

### Likelihood Explanation

Any provider that has not set `defaultGasLimit` (i.e., `defaultGasLimit == 0`) is affected. The `requestWithCallback` legacy entry point passes `gasLimit = 0`, which routes through `requestHelper` and sets `gasLimit10k = 0` for such providers. An unprivileged attacker only needs to deploy a contract with a conditional-revert callback and call `requestWithCallback` — no special permissions are required. The fee cost per attempt is the only barrier, and for high-value applications the attack is economically rational. [7](#0-6) 

---

### Recommendation

1. **Wrap the legacy callback path in `excessivelySafeCall`** (same as the new path) so that a reverting callback does not roll back `clearRequest`. The request should be cleared regardless of callback success, and a `CallbackFailed` event should be emitted.
2. **Alternatively, deprecate and gate the legacy path** by requiring all new requests to use a non-zero `gasLimit` (i.e., require providers to set `defaultGasLimit > 0` before accepting callback requests).
3. **Document** that any provider with `defaultGasLimit == 0` is incompatible with the callback failure recovery flow and is exposed to this attack.

---

### Proof of Concept

```solidity
contract MaliciousRequester is IEntropyConsumer {
    IEntropy entropy;
    address provider;
    uint64 public pendingSeq;
    bytes32 public acceptedRandom;

    // Attacker's winning condition: random number must be < threshold
    bytes32 constant THRESHOLD = bytes32(uint256(1) << 200);

    constructor(address _entropy, address _provider) payable {
        entropy = IEntropy(_entropy);
        provider = _provider;
    }

    function makeRequest(bytes32 userRandom) external payable {
        uint128 fee = entropy.getFee(provider);
        pendingSeq = entropy.requestWithCallback{value: fee}(provider, userRandom);
    }

    // Called by Entropy during revealWithCallback (legacy path, gasLimit10k == 0)
    function entropyCallback(
        uint64 seq,
        address,
        bytes32 randomNumber
    ) internal override {
        // Inspect the random number BEFORE accepting it.
        // If unfavorable, revert — this rolls back clearRequest,
        // leaving the request in-flight. Attacker re-requests.
        require(randomNumber < THRESHOLD, "Unfavorable: reject and re-request");
        acceptedRandom = randomNumber;
    }

    function getEntropy() internal view override returns (address) {
        return address(entropy);
    }
}
```

**Attack flow:**
1. Attacker deploys `MaliciousRequester` against a provider with `defaultGasLimit == 0`.
2. Attacker calls `makeRequest` — fee is paid, request stored with `gasLimit10k = 0`.
3. Provider's keeper calls `revealWithCallback` — random number is computed and passed to `entropyCallback`.
4. If `randomNumber >= THRESHOLD`, callback reverts → entire transaction reverts → `clearRequest` is undone → request stays active.
5. Attacker calls `makeRequest` again with a new `userRandom` to get a new sequence number and a new random number.
6. Repeat until `randomNumber < THRESHOLD` is satisfied.

The attacker pays the request fee per attempt but can bias the outcome of any randomness-dependent application.

### Citations

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L561-566)
```text
        bytes32 randomNumber;
        (randomNumber, ) = revealHelper(
            req,
            userContribution,
            providerContribution
        );
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
