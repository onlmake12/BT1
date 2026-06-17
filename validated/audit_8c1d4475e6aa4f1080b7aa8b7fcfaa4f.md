### Title
Unbounded `callbackGasLimit` in Echo.sol Allows Permanent Fund Locking with Zero Gas Fee - (`File: target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.sol`'s `requestPriceUpdatesWithCallback` accepts any `callbackGasLimit` up to `type(uint32).max` (4,294,967,295) with no upper bound enforcement. When a provider sets `feePerGasInWei = 0`, a user can request a callback gas limit far exceeding the block gas limit (~30M on Ethereum) while paying only the flat base fee. The provider can never execute such a callback, permanently locking the user's fee in the contract. There is no cancel or refund mechanism. This is the direct analog of the GaslessPaymaster issue: an unbounded gas parameter with no per-request cap allows resource exhaustion of a shared service.

---

### Finding Description

`Entropy.sol` defines and enforces a hard cap:

```solidity
uint32 public constant MAX_GAS_LIMIT =
    uint32(type(uint16).max) * TEN_THOUSAND; // 655,350,000
``` [1](#0-0) 

Any `requestV2` call with a `gasLimit` exceeding this value reverts with `MaxGasLimitExceeded`.

`Echo.sol` has **no equivalent cap**. The `callbackGasLimit` field is a raw `uint32` stored directly in the `Request` struct: [2](#0-1) 

In `requestPriceUpdatesWithCallback`, the only fee check is:

```solidity
uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
if (msg.value < requiredFee) revert InsufficientFee();
``` [3](#0-2) 

The fee for gas is computed as:

```solidity
uint96 providerFeeInWei = _state.providers[provider].feePerGasInWei;
uint256 gasFee = callbackGasLimit * providerFeeInWei;
``` [4](#0-3) 

When `feePerGasInWei = 0` (a valid provider configuration — the field is set freely in `registerProvider` with no minimum), `gasFee = 0` regardless of `callbackGasLimit`. A user pays only `baseFeeInWei + feePerFeedInWei * priceIds.length` even for `callbackGasLimit = type(uint32).max`.

At execution time, `executeCallback` forwards exactly `req.callbackGasLimit` gas to the consumer:

```solidity
try
    IEchoConsumer(req.requester)._echoCallback{
        gas: req.callbackGasLimit
    }(sequenceNumber, priceFeeds)
``` [5](#0-4) 

A `callbackGasLimit` of 4,294,967,295 exceeds every chain's block gas limit. No transaction can supply that much gas, so `executeCallback` can never be mined for that request. The user's fee (`req.fee`) is credited to the provider only inside `executeCallback`:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
``` [6](#0-5) 

Because `executeCallback` never runs, the fee is never credited and never returned. Echo.sol has no `cancelRequest`, `refundRequest`, or timeout mechanism, so the ETH is permanently locked.

---

### Impact Explanation

- **User funds permanently locked**: Any ETH sent with a request whose `callbackGasLimit` exceeds the block gas limit is irrecoverable. There is no refund path.
- **Provider service degraded**: With `feePerGasInWei = 0`, an attacker can flood the contract with zero-extra-cost unexecutable requests, filling the 32 fixed request slots and the overflow map, degrading throughput for honest users.
- **No compensation for provider**: The provider never receives the fee for stuck requests, harming their economics.

---

### Likelihood Explanation

- Any unprivileged user can call `requestPriceUpdatesWithCallback` — no special role required.
- Providers setting `feePerGasInWei = 0` is a realistic configuration (flat-fee model). Even with `feePerGasInWei > 0`, any `callbackGasLimit` above the chain's block gas limit (~30M on Ethereum, ~15M on some L2s) produces the same stuck-request outcome.
- The attack requires only paying the flat base fee (potentially a few wei or gwei), making it cheap to execute repeatedly.

---

### Recommendation

Add a maximum `callbackGasLimit` constant in `Echo.sol`, mirroring `Entropy.sol`'s `MAX_GAS_LIMIT`:

```solidity
uint32 public constant MAX_CALLBACK_GAS_LIMIT = 10_000_000; // or chain-appropriate value

// In requestPriceUpdatesWithCallback:
if (callbackGasLimit > MAX_CALLBACK_GAS_LIMIT) revert CallbackGasLimitExceeded();
```

Additionally, consider adding a request cancellation/timeout mechanism so that stuck requests can be refunded after a deadline.

---

### Proof of Concept

1. Provider registers with `feePerGasInWei = 0`, `baseFeeInWei = 1 wei`.
2. Attacker calls:
   ```solidity
   echo.requestPriceUpdatesWithCallback{value: 1 wei}(
       provider,
       block.timestamp,
       priceIds,        // 1 price ID
       type(uint32).max // 4,294,967,295 gas
   );
   ```
   Fee required = `pythFeeInWei + 1 wei + 0 (feePerFeedInWei) + 0 (gasFee)`. Passes.
3. `req.callbackGasLimit = 4,294,967,295` is stored.
4. Provider attempts `executeCallback`. The transaction requires forwarding 4.29B gas to the callback — impossible within any block gas limit. The transaction cannot be included.
5. The attacker's ETH is locked. The request slot is permanently occupied. Repeating step 2 fills the contract with unexecutable requests at minimal cost. [7](#0-6) [8](#0-7)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L73-74)
```text
    uint32 public constant MAX_GAS_LIMIT =
        uint32(type(uint16).max) * TEN_THOUSAND;
```

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L16-17)
```text
        uint32 callbackGasLimit;
        uint96 fee;
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L52-102)
```text
    function requestPriceUpdatesWithCallback(
        address provider,
        uint64 publishTime,
        bytes32[] calldata priceIds,
        uint32 callbackGasLimit
    ) external payable override returns (uint64 requestSequenceNumber) {
        require(
            _state.providers[provider].isRegistered,
            "Provider not registered"
        );

        // FIXME: this comment is wrong. (we're not using tx.gasprice)
        // NOTE: The 60-second future limit on publishTime prevents a DoS vector where
        //      attackers could submit many low-fee requests for far-future updates when gas prices
        //      are low, forcing executors to fulfill them later when gas prices might be much higher.
        //      Since tx.gasprice is used to calculate fees, allowing far-future requests would make
        //      the fee estimation unreliable.
        require(publishTime <= block.timestamp + 60, "Too far in future");
        if (priceIds.length > MAX_PRICE_IDS) {
            revert TooManyPriceIds(priceIds.length, MAX_PRICE_IDS);
        }
        requestSequenceNumber = _state.currentSequenceNumber++;

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

        emit PriceUpdateRequested(req, priceIds);
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-162)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L176-179)
```text
        try
            IEchoConsumer(req.requester)._echoCallback{
                gas: req.callbackGasLimit
            }(sequenceNumber, priceFeeds)
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L235-255)
```text
    function getFee(
        address provider,
        uint32 callbackGasLimit,
        bytes32[] calldata priceIds
    ) public view override returns (uint96 feeAmount) {
        uint96 baseFee = _state.pythFeeInWei; // Fixed fee to Pyth
        // Note: The provider needs to set its fees to include the fee charged by the Pyth contract.
        // Ideally, we would be able to automatically compute the pyth fees from the priceIds, but the
        // fee computation on IPyth assumes it has the full updated data.
        uint96 providerBaseFee = _state.providers[provider].baseFeeInWei;
        uint96 providerFeedFee = SafeCast.toUint96(
            priceIds.length * _state.providers[provider].feePerFeedInWei
        );
        uint96 providerFeeInWei = _state.providers[provider].feePerGasInWei; // Provider's per-gas rate
        uint256 gasFee = callbackGasLimit * providerFeeInWei; // Total provider fee based on gas
        feeAmount =
            baseFee +
            providerBaseFee +
            providerFeedFee +
            SafeCast.toUint96(gasFee); // Total fee user needs to pay
    }
```
