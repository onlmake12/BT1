### Title
Single `requester` Address Used for Both Fee Payment and Callback Target Permanently Locks Entropy Requests — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.sol`, the `req.requester` field is used as both the fee-paying identity and the callback target. In the legacy code path (when `req.gasLimit10k == 0`, i.e., the provider has not set `defaultGasLimit`), `revealWithCallback` makes a raw, uncaught call to `IEntropyConsumer(req.requester)._entropyCallback(...)`. If the requester is a contract that does not implement `IEntropyConsumer`, this call reverts, rolling back `clearRequest` and permanently trapping the request in an unfulfillable state. The user's fee is already consumed (credited to the provider at request time) and there is no cancellation path.

---

### Finding Description

`requestHelper` stores `req.requester = msg.sender` and immediately credits the provider's fee:

```solidity
req.requester = msg.sender;
...
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
``` [1](#0-0) [2](#0-1) 

Later, in `revealWithCallback`, the legacy branch (entered when `req.gasLimit10k == 0`) calls `clearRequest` and then makes a **bare, uncaught** external call to the requester:

```solidity
address callAddress = req.requester;
clearRequest(provider, sequenceNumber);
// WARNING: DO NOT USE req BELOW HERE AS ITS CONTENTS HAS BEEN CLEARED

uint len;
assembly { len := extcodesize(callAddress) }
if (len != 0) {
    IEntropyConsumer(callAddress)._entropyCallback(
        sequenceNumber, provider, randomNumber
    );
}
``` [3](#0-2) 

If `callAddress` is a contract that does not implement `IEntropyConsumer._entropyCallback`, the call reverts. Because `clearRequest` was called in the same transaction, the revert undoes it — the request is **not** cleared. Every subsequent `revealWithCallback` attempt will also revert for the same reason, permanently trapping the request.

The new code path (when `gasLimit10k != 0`) correctly uses `excessivelySafeCall` and moves the request to `CALLBACK_FAILED` state on failure:

```solidity
(success, ret) = req.requester.excessivelySafeCall(
    uint256(req.gasLimit10k) * TEN_THOUSAND,
    256,
    abi.encodeWithSelector(IEntropyConsumer._entropyCallback.selector, ...)
);
``` [4](#0-3) 

But the legacy path remains reachable for any provider that has not set `defaultGasLimit != 0`:

```solidity
if (providerInfo.defaultGasLimit == 0) {
    req.gasLimit10k = 0;
}
``` [5](#0-4) 

There is no `cancelRequest` function in the contract, so the user has no recovery path.

---

### Impact Explanation

- The user's fee is permanently consumed (credited to the provider at request time, before the callback is ever attempted).
- The request slot is permanently occupied and can never be cleared, because every `revealWithCallback` call reverts.
- The user never receives their random number.
- The occupied slot can collide with future requests in the fixed-size `requests[NUM_REQUESTS]` ring buffer, forcing them into the overflow mapping and increasing gas costs. [6](#0-5) 

---

### Likelihood Explanation

- Any contract that calls `requestWithCallback` or `requestV2` without implementing `IEntropyConsumer` triggers this path. This is a realistic integration mistake (e.g., a proxy, a multisig, or a contract that delegates the request but not the callback).
- The legacy path is active for all providers that have not explicitly set `defaultGasLimit`, which includes any provider registered before the new gas-limit flow was introduced.
- No privileged access is required; any unprivileged Entropy user can trigger this by calling `requestWithCallback` from a non-compliant contract. [7](#0-6) 

---

### Recommendation

1. **Separate the callback target from the fee payer**: Add an explicit `callbackReceiver` parameter to `requestWithCallback` / `requestV2`, analogous to how `LSSVMRouter` separates `tokenRecipient` from `nftRecipient`. Store it as `req.callbackReceiver` and use it only for the callback, keeping `req.requester` for fee accounting.

2. **Alternatively, wrap the legacy callback in a try/catch** (or use `excessivelySafeCall`) so that a non-compliant requester contract does not permanently trap the request. Move the request to a `CALLBACK_FAILED` state on failure, consistent with the new code path.

3. **Add an interface check at request time**: Verify that `msg.sender` implements `IEntropyConsumer` (e.g., via ERC-165 or a static call) before accepting a `requestWithCallback`.

---

### Proof of Concept

1. Deploy a contract `BadRequester` that does **not** implement `IEntropyConsumer`.
2. From `BadRequester`, call `entropy.requestWithCallback{value: fee}(provider, userRandomNumber)`. The request is stored with `req.requester = address(BadRequester)` and `req.gasLimit10k = 0` (provider has no `defaultGasLimit`). The fee is credited to the provider.
3. The provider calls `entropy.revealWithCallback(provider, seqNum, userContrib, providerContrib)`.
4. The legacy branch executes: `clearRequest` runs, then `IEntropyConsumer(address(BadRequester))._entropyCallback(...)` reverts (no such function).
5. The entire transaction reverts, undoing `clearRequest`. The request remains active.
6. Repeat step 3 — it always reverts. The request is permanently stuck. The user's fee is gone and they never receive a random number. [8](#0-7) [9](#0-8)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L233-239)
```text
        // Check that fees were paid and increment the pyth / provider balances.
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L256-260)
```text
        }
        req.commitment = keccak256(
            bytes.concat(userCommitment, providerInfo.currentCommitment)
        );
        req.requester = msg.sender;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L268-271)
```text
        if (providerInfo.defaultGasLimit == 0) {
            // Provider doesn't support the new callback failure state flow (toggled by setting the gas limit field).
            // Set gasLimit10k to 0 to disable.
            req.gasLimit10k = 0;
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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L582-596)
```text
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
