### Title
Entropy V2 `CALLBACK_FAILED` State Persists After Random Number Is Publicly Revealed, Enabling Selective Outcome Acceptance — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In Pyth Entropy V2, when a callback-with-gas-limit fails, the computed `randomNumber` is emitted publicly in the `CallbackFailed` event while the request is kept alive in `CALLBACK_FAILED` state. A malicious requester contract can observe the revealed random number and selectively accept or reject it by controlling whether its `entropyCallback` reverts on the retry call. This breaks the fairness guarantee of the randomness protocol.

---

### Finding Description

`revealWithCallback` in `Entropy.sol` implements a two-branch execution path gated on `req.gasLimit10k != 0 && req.callbackStatus == CALLBACK_NOT_STARTED`.

In the new V2 path (lines 574–660):

1. `revealHelper` is called unconditionally at line 562, computing and returning `randomNumber` from the committed `userContribution` and `providerContribution`.
2. The callback is invoked via `excessivelySafeCall` with a bounded gas limit (line 582).
3. If the callback reverts with sufficient gas, the `CallbackFailed` event is emitted **including the `randomNumber`** (lines 630–638), and the request is kept alive with `callbackStatus = CALLBACK_FAILED` (line 651).
4. The guard at lines 553–558 explicitly permits re-entry when `callbackStatus == CALLBACK_FAILED`, so anyone may call `revealWithCallback` again to retry delivery.

The critical invariant violation: **the random number is irrevocably committed and publicly broadcast before the callback session is successfully terminated**. The request state (`CALLBACK_FAILED`) persists indefinitely, and the requester controls whether the retry succeeds.

A malicious requester contract can implement a `setReverts(bool)` flag (exactly as shown in the test helper `EntropyConsumer`). The attack flow:

1. Deploy requester contract with `reverts = true`.
2. Call `requestV2` with a non-zero gas limit → `gasLimit10k != 0`.
3. Provider calls `revealWithCallback` → callback reverts → `CallbackFailed` event emitted with `randomNumber`.
4. Attacker reads `randomNumber` from the event log.
5. If favorable: call `setReverts(false)`, then call `revealWithCallback` again → callback succeeds → request cleared.
6. If unfavorable: keep `reverts = true` → request stays in `CALLBACK_FAILED` forever (or until a new request is made).

The random number for a given request is deterministic and fixed (`combineRandomValues(userContribution, providerContribution, 0)`), so the attacker cannot get a different number — but they can choose whether to accept or reject the known outcome.

---

### Impact Explanation

Any application built on Entropy V2 with callback-gas-limit support (i.e., `requestV2` with `gasLimit > 0` or a provider with `defaultGasLimit != 0`) is vulnerable to outcome manipulation by a malicious requester. Affected use cases include lotteries, NFT trait randomization, on-chain games, and any protocol where the requester controls the callback contract. The attacker can guarantee they only accept random numbers that are favorable to them, completely undermining the fairness guarantee of the randomness service.

**Impact: High** — direct manipulation of randomness outcomes for any Entropy V2 callback-based application.

---

### Likelihood Explanation

The attack requires:
- Deploying a requester contract with a conditional revert flag (trivial).
- Calling `requestV2` with a gas limit (standard usage).
- Reading the `CallbackFailed` event (public on-chain data).
- Calling `revealWithCallback` again after flipping the flag (permissionless — anyone can call it).

No privileged access, leaked keys, or external oracle manipulation is required. The entire attack is executable by an unprivileged Entropy user. The test suite itself (`EntropyConsumer.setReverts`) demonstrates the exact mechanism.

**Likelihood: Medium** — requires a deliberately malicious requester contract, but the implementation is trivial and the economic incentive is high for any application with valuable randomness outcomes.

---

### Recommendation

The root cause is that `randomNumber` is computed and emitted before the callback session is confirmed complete. Mitigations include:

1. **Do not emit `randomNumber` in `CallbackFailed`**: Emit only the commitment hash or omit the random number from the failure event. The random number should only be observable after successful delivery.
2. **Commit-reveal separation**: Store a hash of the random number on first failure; only reveal the preimage on successful callback delivery.
3. **Restrict retry caller**: Limit `revealWithCallback` retries (when `callbackStatus == CALLBACK_FAILED`) to the original provider or a permissioned keeper, preventing the requester from controlling retry timing.
4. **Timeout/expiry on `CALLBACK_FAILED`**: After a configurable number of blocks, automatically clear the request and refund the fee, preventing indefinite selective-acceptance loops.

---

### Proof of Concept

```solidity
// Malicious requester contract
contract MaliciousRequester is IEntropyConsumer {
    IEntropyV2 public entropy;
    bool public reverts = true;
    bytes32 public lastRandomNumber;

    constructor(address _entropy) { entropy = IEntropyV2(_entropy); }

    function requestRandom() external payable returns (uint64) {
        uint128 fee = entropy.getFeeV2();
        return entropy.requestV2{value: fee}();
    }

    function setReverts(bool _reverts) external { reverts = _reverts; }

    function entropyCallback(
        uint64, address, bytes32 randomNumber
    ) internal override {
        if (reverts) revert("selective reject");
        lastRandomNumber = randomNumber;
    }

    function getEntropy() internal view override returns (address) {
        return address(entropy);
    }
}

// Attack sequence:
// 1. Deploy MaliciousRequester (reverts = true)
// 2. Call requestRandom() → get sequenceNumber
// 3. Provider calls revealWithCallback() → CallbackFailed event emitted with randomNumber
// 4. Read randomNumber from event
// 5. If favorable: malicious.setReverts(false); revealWithCallback(...) → callback succeeds
// 6. If unfavorable: leave reverts = true; request stays CALLBACK_FAILED
```

The `CallbackFailed` event at lines 630–638 of `Entropy.sol` broadcasts `randomNumber` publicly: [1](#0-0) 

The guard at lines 553–558 explicitly permits retry when `CALLBACK_FAILED`, giving the requester a second chance after observing the outcome: [2](#0-1) 

The `CALLBACK_FAILED` constant is defined as a persistent terminal state (not auto-cleared): [3](#0-2)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L553-558)
```text
        if (
            !(req.callbackStatus ==
                EntropyStatusConstants.CALLBACK_NOT_STARTED ||
                req.callbackStatus == EntropyStatusConstants.CALLBACK_FAILED)
        ) {
            revert EntropyErrors.InvalidRevealCall();
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L630-651)
```text
                emit CallbackFailed(
                    provider,
                    req.requester,
                    sequenceNumber,
                    userContribution,
                    providerContribution,
                    randomNumber,
                    ret
                );
                emit EntropyEventsV2.Revealed(
                    provider,
                    req.requester,
                    sequenceNumber,
                    randomNumber,
                    userContribution,
                    providerContribution,
                    true,
                    ret,
                    SafeCast.toUint32(gasUsed),
                    bytes("")
                );
                req.callbackStatus = EntropyStatusConstants.CALLBACK_FAILED;
```

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyStatusConstants.sol (L11-12)
```text
    // A request with callback where the callback has been invoked and failed.
    uint8 public constant CALLBACK_FAILED = 3;
```
