### Title
User-Paid Fees Permanently Locked in Echo Contract When Provider Fails to Execute Callback — (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary
In `Echo.sol`, when a user calls `requestPriceUpdatesWithCallback` and pays a fee, the provider portion of that fee (`req.fee`) is stored in the request struct. There is no cancellation or refund mechanism. If the assigned provider never calls `executeCallback`, the user's fee is permanently locked in the contract with no recovery path.

### Finding Description

When `requestPriceUpdatesWithCallback` is called, the user pays `msg.value`. The Pyth protocol fee is immediately credited to `_state.accruedFeesInWei`, and the remainder is stored in `req.fee`: [1](#0-0) 

```solidity
uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
if (msg.value < requiredFee) revert InsufficientFee();
// ...
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
// ...
_state.accruedFeesInWei += _state.pythFeeInWei;
```

The provider fee (`req.fee`) is only ever released when `executeCallback` is called successfully, crediting the provider: [2](#0-1) 

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
clearRequest(sequenceNumber);
```

`clearRequest` is **only** called from `executeCallback`. There is no `cancelRequest`, no admin refund function, and no timeout-based recovery. If the provider never calls `executeCallback`, `req.fee` is permanently locked in the contract.

The developers themselves acknowledge this in code comments: [3](#0-2) 

```solidity
// TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
// If executeCallback can revert, then funds can be permanently locked in the contract.
// TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
```

Additionally, during the exclusivity period, **only the assigned provider** can call `executeCallback`: [4](#0-3) 

This means that if the assigned provider is offline or malicious during the exclusivity window, no other actor can fulfill the request and the user's fee is stuck.

The `Request` struct stores the fee per-request: [5](#0-4) 

There are only `NUM_REQUESTS = 32` slots. Permanently unfulfilled requests also consume these slots, potentially causing a secondary DoS. [6](#0-5) 

### Impact Explanation
Any user who calls `requestPriceUpdatesWithCallback` and pays a fee loses those funds permanently if the provider does not execute the callback. There is no admin function, no timeout, and no user-initiated cancellation to recover the locked `req.fee`. This is a direct, irreversible financial loss for users.

### Likelihood Explanation
Providers can go offline, be deregistered, or simply fail to respond. The exclusivity period enforced by `exclusivityPeriodSeconds` (default 15 seconds) means that during that window, no substitute provider can step in. Any network disruption, provider bug, or deliberate provider inaction during the exclusivity window results in permanently locked user funds. This is a realistic operational scenario, not a theoretical one.

### Recommendation
Implement a user-callable `cancelRequest` function that:
1. Checks that the request has not been fulfilled (i.e., it is still active).
2. Enforces a minimum timeout (e.g., after the exclusivity period has elapsed plus a grace period).
3. Refunds `req.fee` to `req.requester`.
4. Calls `clearRequest` to free the slot.

Alternatively, implement an automatic refund path inside `executeCallback` if the callback fails, returning `req.fee` to the requester rather than crediting the provider.

### Proof of Concept

1. User calls `requestPriceUpdatesWithCallback{value: fee}(provider, publishTime, priceIds, gasLimit)`.
2. `req.fee = msg.value - pythFeeInWei` is stored in the request struct.
3. The assigned `provider` goes offline or ignores the request.
4. The exclusivity period (`exclusivityPeriodSeconds`) passes; no other provider can retroactively fulfill (they can after the period, but if the request slot is stale and no one monitors it, it remains unfulfilled).
5. There is no function the user can call to recover `req.fee`.
6. The fee is permanently locked in the `Echo` contract, accessible only to the provider (who never fulfills) via `accruedFeesInWei` — but since `clearRequest` was never called, `req.fee` was never added to `accruedFeesInWei` either. The ETH is simply stranded in the contract balance with no accounting entry pointing to it.

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L75-99)
```text
        uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
        if (msg.value < requiredFee) revert InsufficientFee();

        Request storage req = allocRequest(requestSequenceNumber);
        req.sequenceNumber = requestSequenceNumber;
        req.publishTime = publishTime;
        req.callbackGasLimit = callbackGasLimit;
        req.requester = msg.sender;
        req.provider = provider;
        req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);

        // Create array with the right size
        req.priceIdPrefixes = new bytes8[](priceIds.length);

        // Copy only the first 8 bytes of each price ID to storage
        for (uint8 i = 0; i < priceIds.length; i++) {
            // Extract first 8 bytes of the price ID
            bytes32 priceId = priceIds[i];
            bytes8 prefix;
            assembly {
                prefix := priceId
            }
            req.priceIdPrefixes[i] = prefix;
        }
        _state.accruedFeesInWei += _state.pythFeeInWei;
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L113-121)
```text
        // Check provider exclusivity using configurable period
        if (
            block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds
        ) {
            require(
                providerToCredit == req.provider,
                "Only assigned provider during exclusivity period"
            );
        }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L155-160)
```text
        // TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
        // If executeCallback can revert, then funds can be permanently locked in the contract.
        // TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
        // This should take funds from the expected provider and give to providerToCredit. The penalty should probably scale
        // with time in order to ensure that the callback eventually gets executed.
        // (There may be exploits with ^ though if the consumer contract is malicious ?)
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-164)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);

        clearRequest(sequenceNumber);
```

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L6-7)
```text
    uint8 public constant NUM_REQUESTS = 32;
    bytes1 public constant NUM_REQUESTS_MASK = 0x1f;
```

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L12-29)
```text
    struct Request {
        // Slot 1: 8 + 8 + 4 + 12 = 32 bytes
        uint64 sequenceNumber;
        uint64 publishTime;
        uint32 callbackGasLimit;
        uint96 fee;
        // Slot 2: 20 + 12 = 32 bytes
        address requester;
        // 12 bytes padding

        // Slot 3: 20 + 12 = 32 bytes
        address provider;
        // 12 bytes padding

        // Dynamic array starts at its own slot
        // Store only first 8 bytes of each price ID to save gas
        bytes8[] priceIdPrefixes;
    }
```
