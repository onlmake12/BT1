### Title
Permissionless `revealWithCallback` with Publicly Emitted `userRandomNumber` Enables Front-Running Griefing of Provider Fulfillment — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `revealWithCallback` function in Pyth Entropy is explicitly permissionless — anyone can call it. The `userRandomNumber` (the user's secret contribution) is publicly emitted in the `RequestedWithCallback` event at request time, and the provider's revelation is publicly available from the Fortuna API. An unprivileged attacker can monitor the mempool for a provider's pending `revealWithCallback` transaction, front-run it with carefully calibrated gas, and force the request into `CALLBACK_FAILED` state. This disrupts the provider's automated fulfillment path (Fortuna), changes the callback invocation semantics from safe (try-catch) to direct (no try-catch), and can cause the provider's subsequent recovery attempts to revert if the callback is gas-sensitive.

---

### Finding Description

**Root cause 1 — `userRandomNumber` is publicly emitted at request time:** [1](#0-0) 

The `userContribution` (the raw `userRandomNumber`) is emitted in both `RequestedWithCallback` and `EntropyEventsV2.Requested` events. Anyone monitoring the chain can extract it from the event log.

**Root cause 2 — `revealWithCallback` is explicitly permissionless:** [2](#0-1) 

The function comment states "Anyone can call this method to fulfill a request." There is no `msg.sender` check, unlike `reveal` which enforces `req.requester == msg.sender`. [3](#0-2) 

**Root cause 3 — Callback failure state machine is one-way per call:**

When `gasLimit10k != 0` and `callbackStatus == CALLBACK_NOT_STARTED`, the contract uses `excessivelySafeCall` (try-catch). If the callback fails with sufficient gas, the status transitions to `CALLBACK_FAILED`: [4](#0-3) 

Once in `CALLBACK_FAILED`, the recovery path invokes the callback **directly** (no try-catch), meaning any revert in the callback propagates and reverts the entire `revealWithCallback` transaction: [5](#0-4) 

---

### Impact Explanation

**Attack flow:**

1. User calls `requestWithCallback` / `requestV2` with `gasLimit10k != 0`. The `userRandomNumber` is emitted publicly in the `RequestedWithCallback` event.
2. Provider (Fortuna) prepares a `revealWithCallback` transaction with the correct `userContribution` and `providerContribution`.
3. Attacker monitors the mempool, extracts both values (from the event log and the Fortuna public API endpoint `/revelations/{sequenceNumber}`).
4. Attacker front-runs the provider's transaction with the same parameters but with gas calibrated to pass the `(startingGas * 31) / 32 > gasLimit10k * 10000` check while still causing the callback to fail (e.g., by targeting a gas-sensitive callback).
5. The callback fails → `callbackStatus` transitions to `CALLBACK_FAILED`. The `CallbackFailed` event is emitted.
6. The provider's original transaction now fails because `callbackStatus` is `CALLBACK_FAILED`, not `CALLBACK_NOT_STARTED`.
7. The provider must retry in recovery mode (the `else` branch), where the callback is invoked directly without try-catch. If the callback reverts for any reason, the provider's recovery transaction reverts entirely.

**Concrete impact:**
- Provider's automated fulfillment service (Fortuna) is disrupted; its transactions fail and it wastes gas.
- The request is stuck in `CALLBACK_FAILED` state until the provider or user manually retries.
- In recovery mode, a gas-sensitive callback that would have succeeded under the provider's normal gas budget can be made to revert, permanently blocking automated recovery.
- The user's application does not receive its randomness callback in a timely manner. [6](#0-5) 

---

### Likelihood Explanation

- The `userRandomNumber` is emitted in a public event at request time, making it trivially extractable by any chain monitor.
- The provider's revelation is publicly available from the Fortuna HTTP API (`/revelations/{sequenceNumber}`), as documented in the debug tooling: [7](#0-6) 

- `revealWithCallback` requires no special role or key — any EOA can call it.
- The attacker only needs to calibrate gas to pass the `31/32` check while causing the callback to fail, which is feasible for any callback with non-trivial gas usage.
- The attack is repeatable: each time the provider retries, the attacker can front-run again.

---

### Recommendation

1. **Add an optional `msg.sender` restriction**: Allow the requester to designate a trusted fulfiller address at request time. Only that address (or the requester themselves) can call `revealWithCallback` for that request.
2. **Alternatively, add a minimum gas enforcement at the `revealWithCallback` entry point**: Revert early if `gasleft()` is below `gasLimit10k * 10000 + overhead`, preventing the attacker from calibrating gas to force `CALLBACK_FAILED`.
3. **Do not emit the raw `userRandomNumber` in the request event**: Emit only the commitment (`keccak256(userRandomNumber)`) in the event. The raw value is only needed at reveal time and should be kept off-chain until then. This removes the attacker's ability to reconstruct the full `revealWithCallback` call parameters.

---

### Proof of Concept

```
1. User (consumer contract) calls requestV2(provider, userRandomNumber=0xABCD..., gasLimit=100000)
   → Event emitted: RequestedWithCallback(provider, consumer, seqNum=5, userContribution=0xABCD...)

2. Attacker reads userContribution=0xABCD... from the event log.
   Attacker queries https://fortuna.provider.com/revelations/5 → providerContribution=0xDEF0...

3. Provider submits: revealWithCallback(provider, 5, 0xABCD..., 0xDEF0...) with gas=200000

4. Attacker front-runs: revealWithCallback(provider, 5, 0xABCD..., 0xDEF0...) with gas=120000
   → excessivelySafeCall forwards ~115000 gas to callback
   → callback uses 110000 gas and fails (out of gas)
   → (startingGas * 31/32) = 116250 > gasLimit10k * 10000 = 100000 → CALLBACK_FAILED branch taken
   → callbackStatus = CALLBACK_FAILED, CallbackFailed event emitted

5. Provider's original tx executes:
   → callbackStatus == CALLBACK_FAILED → else branch taken (direct invocation, no try-catch)
   → clearRequest called, then callback invoked directly
   → callback reverts (still out of gas in this context) → entire tx reverts
   → Provider's tx fails, wasted gas

6. Provider retries → attacker front-runs again → loop continues
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L374-389)
```text
        emit RequestedWithCallback(
            provider,
            req.requester,
            req.sequenceNumber,
            userContribution,
            EntropyStructConverter.toV1Request(req)
        );
        emit EntropyEventsV2.Requested(
            provider,
            req.requester,
            req.sequenceNumber,
            userContribution,
            uint32(req.gasLimit10k) * TEN_THOUSAND,
            bytes("")
        );
        return req.sequenceNumber;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L493-515)
```text
    //
    // This function must be called by the same `msg.sender` that originally requested the random number. This check
    // prevents denial-of-service attacks where another actor front-runs the requester's reveal transaction.
    function reveal(
        address provider,
        uint64 sequenceNumber,
        bytes32 userContribution,
        bytes32 providerContribution
    ) public override returns (bytes32 randomNumber) {
        EntropyStructsV2.Request storage req = findActiveRequest(
            provider,
            sequenceNumber
        );

        if (
            req.callbackStatus != EntropyStatusConstants.CALLBACK_NOT_NECESSARY
        ) {
            revert EntropyErrors.InvalidRevealCall();
        }

        if (req.requester != msg.sender) {
            revert EntropyErrors.Unauthorized();
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L541-547)
```text
    // Anyone can call this method to fulfill a request, but the callback will only be made to the original requester.
    function revealWithCallback(
        address provider,
        uint64 sequenceNumber,
        bytes32 userContribution,
        bytes32 providerContribution
    ) public override {
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L553-559)
```text
        if (
            !(req.callbackStatus ==
                EntropyStatusConstants.CALLBACK_NOT_STARTED ||
                req.callbackStatus == EntropyStatusConstants.CALLBACK_FAILED)
        ) {
            revert EntropyErrors.InvalidRevealCall();
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-651)
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

            if (success) {
                emit RevealedWithCallback(
                    EntropyStructConverter.toV1Request(req),
                    userContribution,
                    providerContribution,
                    randomNumber
                );
                emit EntropyEventsV2.Revealed(
                    provider,
                    req.requester,
                    req.sequenceNumber,
                    randomNumber,
                    userContribution,
                    providerContribution,
                    false,
                    ret,
                    SafeCast.toUint32(gasUsed),
                    bytes("")
                );
                clearRequest(provider, sequenceNumber);
            } else if (
                (startingGas * 31) / 32 >
                uint256(req.gasLimit10k) * TEN_THOUSAND
            ) {
                // The callback reverted for some reason.
                // We don't use ret to condition the behavior here (out-of-gas or other revert), as we have found that some user contracts
                // catch out-of-gas errors and revert with a different error.
                // In this case, ensure that the callback was provided with sufficient gas. Technically, 63/64ths of the startingGas is forwarded,
                // but we're using 31/32 to introduce a margin of safety.
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

**File:** contract_manager/scripts/entropy_debug_reveal.ts (L85-97)
```typescript
    const revealUrl = providerInfo.uri + `/revelations/${sequenceNumber}`;
    const fortunaResponse = await fetch(revealUrl);
    if (fortunaResponse.status !== 200) {
      console.error("Fortuna response status:", fortunaResponse.status);
      console.error("Fortuna response body:", await fortunaResponse.text());
      console.error(
        "Refusing to continue the script, please check the Fortuna service first.",
      );
      return;
    }
    const payload = await fortunaResponse.json();
    // @ts-expect-error - TODO payload.value is unknown and the typing needs to be fixed
    const providerRevelation = "0x" + payload.value.data;
```
