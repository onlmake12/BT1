### Title
Legacy `revealWithCallback` Path Permanently Locks Entropy Requests When Requester Lacks `IEntropyConsumer` Interface — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.sol`, the `revealWithCallback` function contains a legacy execution path (triggered when `req.gasLimit10k == 0`) that calls `IEntropyConsumer(callAddress)._entropyCallback(...)` directly with no error catching. If the requester contract does not implement `IEntropyConsumer`, this call reverts, causing the entire `revealWithCallback` transaction to revert. Because the Entropy contract does not validate the `IEntropyConsumer` interface at request time, such requests become permanently unfulfillable — the user's paid fee is locked forever and the provider cannot complete the reveal.

---

### Finding Description

`Entropy.sol` has two distinct paths inside `revealWithCallback`:

**New path** (`req.gasLimit10k != 0` AND `callbackStatus == CALLBACK_NOT_STARTED`): uses `excessivelySafeCall` to catch reverts gracefully, recording failures as `CALLBACK_FAILED` for retry.

**Old/legacy path** (`req.gasLimit10k == 0` OR `callbackStatus == CALLBACK_FAILED`): calls `_entropyCallback` directly with no error catching:

```solidity
if (len != 0) {
    IEntropyConsumer(callAddress)._entropyCallback(
        sequenceNumber,
        provider,
        randomNumber
    );
}
```

The `gasLimit10k == 0` condition is set in `requestHelper` when the provider has not configured a `defaultGasLimit`:

```solidity
if (providerInfo.defaultGasLimit == 0) {
    // Provider doesn't support the new callback failure state flow
    req.gasLimit10k = 0;
}
```

The Entropy contract imposes **no on-chain check** that the requester implements `IEntropyConsumer` at the time of `requestWithCallback` or `requestV2`. Any contract — including one that does not implement `_entropyCallback` — can successfully call `requestWithCallback` and pay the fee.

When the provider later calls `revealWithCallback`:
1. `revealHelper` validates the proofs (no state change yet).
2. `clearRequest` deletes the stored request.
3. `IEntropyConsumer(callAddress)._entropyCallback(...)` is called — **reverts** because the function does not exist on the requester.
4. The entire transaction reverts, including `clearRequest`, so the request is restored to storage.
5. Every subsequent `revealWithCallback` attempt also reverts identically.
6. `reveal` (non-callback path) cannot be used — it reverts with `InvalidRevealCall` for any request with `callbackStatus != CALLBACK_NOT_NECESSARY`.

The request is permanently stuck. The user's fee is permanently locked in the contract.

---

### Impact Explanation

- **Permanent locking of user funds**: fees paid for entropy requests that can never be fulfilled are irrecoverable.
- **Provider DoS**: the provider's keeper cannot complete the reveal for affected requests, wasting gas on every retry.
- **No recovery path**: `reveal` is blocked for callback requests; there is no admin escape hatch to clear stuck requests.

This is a direct analog to the external report: just as the Synthetix wrapper on OP Chain lacks `mint()` causing liquidation type 2 to permanently revert, a requester contract lacking `_entropyCallback` causes `revealWithCallback` to permanently revert on the legacy path.

---

### Likelihood Explanation

- Any provider that has not called `setDefaultGasLimit` (i.e., `defaultGasLimit == 0`) triggers the legacy path for all requests. This includes older providers that predate the `gasLimit` feature.
- Any contract that calls `requestWithCallback` without inheriting `IEntropyConsumer` — a realistic mistake given the interface is only documented, not enforced — will produce a permanently stuck request.
- The combination is realistic: legacy providers are live on mainnet, and the interface requirement is easy to miss.

---

### Recommendation

1. **At request time**, validate that the requester implements `IEntropyConsumer` by checking for `_entropyCallback` selector support (e.g., via ERC-165 or a low-level `staticcall` probe).
2. **In the legacy path**, wrap the `_entropyCallback` call in a `try/catch` or use `excessivelySafeCall` (already imported) to prevent permanent lockup, consistent with the new path.
3. **Alternatively**, deprecate the legacy path entirely by requiring all providers to set a non-zero `defaultGasLimit`.

---

### Proof of Concept

1. Provider `P` has `defaultGasLimit == 0` (legacy provider, never called `setDefaultGasLimit`).
2. Attacker deploys `BadConsumer` — a contract that calls `requestWithCallback` but does **not** inherit `IEntropyConsumer` (no `_entropyCallback` function).
3. `BadConsumer` calls `entropy.requestWithCallback{value: fee}(provider, userRandomNumber)`. This succeeds; `req.gasLimit10k = 0` is stored.
4. Provider's keeper calls `entropy.revealWithCallback(provider, seq, userContrib, providerContrib)`.
5. Inside the legacy `else` branch: `clearRequest` runs, then `IEntropyConsumer(badConsumer)._entropyCallback(...)` reverts (no such function).
6. Entire transaction reverts; request is restored.
7. Every subsequent `revealWithCallback` call reverts identically.
8. `reveal` reverts with `InvalidRevealCall` (request has `callbackStatus == CALLBACK_NOT_STARTED`, not `CALLBACK_NOT_NECESSARY`).
9. The fee paid by `BadConsumer` is permanently locked in the Entropy contract.

**Key code references:**

`requestHelper` sets `gasLimit10k = 0` for legacy providers: [1](#0-0) 

`revealWithCallback` legacy path calls `_entropyCallback` without error catching: [2](#0-1) 

`reveal` is blocked for callback requests: [3](#0-2) 

`IEntropyConsumer._entropyCallback` — the interface the requester must implement: [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L268-272)
```text
        if (providerInfo.defaultGasLimit == 0) {
            // Provider doesn't support the new callback failure state flow (toggled by setting the gas limit field).
            // Set gasLimit10k to 0 to disable.
            req.gasLimit10k = 0;
        } else {
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L507-511)
```text
        if (
            req.callbackStatus != EntropyStatusConstants.CALLBACK_NOT_NECESSARY
        ) {
            revert EntropyErrors.InvalidRevealCall();
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
