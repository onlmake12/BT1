### Title
Echo Callback Does Not Pass Provider Identity to Consumer — (`target_chains/ethereum/contracts/contracts/echo/IEcho.sol`)

### Summary

`Echo.executeCallback()` invokes `IEchoConsumer._echoCallback()` with only `sequenceNumber` and `priceFeeds`. It omits the identity of the provider that fulfilled the request. Unlike the analogous Entropy callback — which explicitly passes `provider` — Echo consumers have no on-chain way to determine which provider fulfilled their request, and the request record is already deleted before the callback fires.

### Finding Description

`Echo.executeCallback()` clears the request from storage before invoking the consumer callback:

```solidity
// Echo.sol line 164 — request cleared BEFORE callback
clearRequest(sequenceNumber);

// Echo.sol lines 176-179 — callback invoked with no provider info
IEchoConsumer(req.requester)._echoCallback{
    gas: req.callbackGasLimit
}(sequenceNumber, priceFeeds)
```

The `IEchoConsumer` interface defines the callback as:

```solidity
// IEcho.sol lines 31-34
function echoCallback(
    uint64 sequenceNumber,
    PythStructs.PriceFeed[] memory priceFeeds
) internal virtual;
```

Neither the originally assigned provider (`req.provider`) nor the actual fulfiller (`providerToCredit`) is forwarded. Because `clearRequest` runs first, the consumer cannot recover this information by re-reading storage.

By contrast, Entropy's analogous callback explicitly passes the provider:

```solidity
// IEntropyConsumer.sol lines 28-32
function entropyCallback(
    uint64 sequence,
    address provider,   // <-- present in Entropy, absent in Echo
    bytes32 randomNumber
) internal virtual;
```

After the configurable exclusivity period expires, **any registered provider** may call `executeCallback` and receive the fee credit. The consumer has no mechanism to distinguish the originally assigned provider from a substitute fulfiller.

### Impact Explanation

- A consumer that registers with multiple providers, or that applies different trust/logic per provider, cannot implement that logic safely: the callback gives no provider identity and the request is already gone from storage.
- After the exclusivity window, a substitute provider can fulfill the request. The consumer's `echoCallback` receives identical arguments regardless of who fulfilled it, making provider-conditional logic impossible.
- Consumers that need to verify the expected provider fulfilled the request must implement complex off-chain workarounds (e.g., storing the provider at request time and correlating by sequence number), which most integrators will not do.

### Likelihood Explanation

- `executeCallback` is permissionless after the exclusivity period; any registered provider can call it.
- Every Echo consumer is affected by the missing parameter — it is a structural omission in the interface, not a configuration error.
- The pattern is easy to miss because the `_echoCallback` wrapper already enforces `msg.sender == echo`, giving developers a false sense that all necessary context is authenticated and present.

### Recommendation

Add the fulfilling provider address as a parameter to both `_echoCallback` and `echoCallback`, mirroring the Entropy design:

```solidity
function _echoCallback(
    uint64 sequenceNumber,
    address provider,                          // add this
    PythStructs.PriceFeed[] memory priceFeeds
) external { ... }

function echoCallback(
    uint64 sequenceNumber,
    address provider,                          // add this
    PythStructs.PriceFeed[] memory priceFeeds
) internal virtual;
```

Pass `providerToCredit` (the actual fulfiller) from `Echo.executeCallback()` into the callback invocation. This allows consumers to authenticate and differentiate fulfillments without requiring off-chain state.

### Proof of Concept

1. Consumer contract `C` calls `requestPriceUpdatesWithCallback(providerA, ...)` and stores `providerA` as the expected provider.
2. The exclusivity period elapses.
3. `providerB` (a different registered provider) calls `executeCallback(providerB, sequenceNumber, ...)`.
4. Echo clears the request (`clearRequest(sequenceNumber)`) and then calls `C._echoCallback(sequenceNumber, priceFeeds)`.
5. Inside `C.echoCallback`, there is no `provider` argument and `echo.getRequest(sequenceNumber)` returns an empty struct (already cleared).
6. `C` cannot determine that `providerB` — not `providerA` — fulfilled the request, and any provider-conditional logic silently executes under the wrong assumption.

**Root cause lines:** [1](#0-0) [2](#0-1) [3](#0-2) 

**Entropy comparison (provider IS passed):** [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L13-22)
```text
    function _echoCallback(
        uint64 sequenceNumber,
        PythStructs.PriceFeed[] memory priceFeeds
    ) external {
        address echo = getEcho();
        require(echo != address(0), "Echo address not set");
        require(msg.sender == echo, "Only Echo can call this function");

        echoCallback(sequenceNumber, priceFeeds);
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L31-34)
```text
    function echoCallback(
        uint64 sequenceNumber,
        PythStructs.PriceFeed[] memory priceFeeds
    ) internal virtual;
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L164-179)
```text
        clearRequest(sequenceNumber);

        // TODO: I'm pretty sure this is going to use a lot of gas because it's doing a storage lookup for each sequence number.
        // a better solution would be a doubly-linked list of active requests.
        // After successful callback, update firstUnfulfilledSeq if needed
        while (
            _state.firstUnfulfilledSeq < _state.currentSequenceNumber &&
            !isActive(findRequest(_state.firstUnfulfilledSeq))
        ) {
            _state.firstUnfulfilledSeq++;
        }

        try
            IEchoConsumer(req.requester)._echoCallback{
                gas: req.callbackGasLimit
            }(sequenceNumber, priceFeeds)
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyConsumer.sol (L28-32)
```text
    function entropyCallback(
        uint64 sequence,
        address provider,
        bytes32 randomNumber
    ) internal virtual;
```
