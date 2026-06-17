### Title
Echo `executeCallback` Fee Calculation Uses Static `feePerGasInWei` Instead of Dynamic `tx.gasprice`, Leaving Providers Under-Compensated During Gas Spikes - (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

The `Echo` contract's fee mechanism locks in a static `feePerGasInWei` rate at provider registration time. When gas prices spike between request submission and callback execution, the pre-paid fee no longer covers the provider's actual execution cost, removing their economic incentive to call `executeCallback`. Pending requests can become permanently stuck.

---

### Finding Description

In `Echo.sol`, the fee charged to users is computed by `getFee()`:

```solidity
uint96 providerFeeInWei = _state.providers[provider].feePerGasInWei;
uint256 gasFee = callbackGasLimit * providerFeeInWei;
feeAmount = baseFee + providerBaseFee + providerFeedFee + SafeCast.toUint96(gasFee);
``` [1](#0-0) 

`feePerGasInWei` is a static value set by the provider at registration time via `registerProvider()` and is not updated per-request. [2](#0-1) 

The fee is locked into the request at submission time:

```solidity
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
``` [3](#0-2) 

When `executeCallback` is called, the provider receives exactly `req.fee + msg.value - pythFee` — the pre-locked amount — regardless of the actual `tx.gasprice` at execution time:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
``` [4](#0-3) 

The code itself contains an explicit developer acknowledgment of this flaw:

```solidity
// FIXME: this comment is wrong. (we're not using tx.gasprice)
// NOTE: The 60-second future limit on publishTime prevents a DoS vector where
//      attackers could submit many low-fee requests for far-future updates when gas prices
//      are low, forcing executors to fulfill them later when gas prices might be much higher.
//      Since tx.gasprice is used to calculate fees, allowing far-future requests would make
//      the fee estimation unreliable.
``` [5](#0-4) 

The comment describes a DoS concern about gas price spikes but then admits `tx.gasprice` is **not actually used** in the fee calculation. The 60-second `publishTime` cap was added as a partial mitigation, but it does not eliminate the risk: gas prices can spike dramatically within 60 seconds on congested chains.

Additionally, unlike the `Scheduler` contract which explicitly adds a `GAS_OVERHEAD` constant to cover the overhead of the execution function itself:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
``` [6](#0-5) 

Echo has no such overhead component. The `callbackGasLimit` only covers the consumer's callback, not the gas consumed by `executeCallback` itself (storage reads, Pyth fee payment, prefix verification loop, `firstUnfulfilledSeq` scan, etc.). [7](#0-6) 

---

### Impact Explanation

If `tx.gasprice` at execution time significantly exceeds the provider's static `feePerGasInWei`, the provider's actual ETH cost to call `executeCallback` exceeds the fee they will receive. Rational providers will decline to execute unprofitable callbacks. Affected requests remain in the `_state.requests` ring buffer indefinitely, blocking the consumer's price update and potentially locking the pre-paid fee in the contract with no cancellation path.

The contract's own TODO comment acknowledges there is no penalty mechanism to force execution:

```solidity
// TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
``` [8](#0-7) 

---

### Likelihood Explanation

Gas price spikes of 10–100× are historically common on Ethereum and L2s during periods of high demand. A provider who set `feePerGasInWei = 1 gwei` during a quiet period will be unprofitable if `tx.gasprice` rises to 10 gwei at execution time. The 60-second `publishTime` cap reduces but does not eliminate the window. Any request submitted just before a spike is vulnerable.

---

### Recommendation

Replace the static `feePerGasInWei` gas component with a dynamic calculation at execution time, analogous to the `Scheduler` pattern:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
```

Alternatively, record `tx.gasprice` at request time and use it as a floor, or implement a fee-adjustment mechanism (similar to Fortuna's `adjust_fee_if_necessary`) that allows providers to update their rate and have it reflected in the fee charged to users.

---

### Proof of Concept

1. Provider registers with `feePerGasInWei = 1 gwei`.
2. User calls `requestPriceUpdatesWithCallback` with `callbackGasLimit = 500_000`. Fee paid = `500_000 * 1 gwei = 0.0005 ETH`.
3. Gas price spikes to 50 gwei before the request is executed.
4. Provider's actual cost to call `executeCallback` = `~500_000 * 50 gwei = 0.025 ETH` (plus overhead).
5. Provider would receive only `0.0005 ETH` — a 50× loss.
6. Provider skips the callback. The request sits in the ring buffer. The consumer never receives its price update. [9](#0-8) [10](#0-9)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L63-68)
```text
        // FIXME: this comment is wrong. (we're not using tx.gasprice)
        // NOTE: The 60-second future limit on publishTime prevents a DoS vector where
        //      attackers could submit many low-fee requests for far-future updates when gas prices
        //      are low, forcing executors to fulfill them later when gas prices might be much higher.
        //      Since tx.gasprice is used to calculate fees, allowing far-future requests would make
        //      the fee estimation unreliable.
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L84-84)
```text
        req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L104-162)
```text
    // TODO: does this need to be payable? Any cost paid to Pyth could be taken out of the provider's accrued fees.
    function executeCallback(
        address providerToCredit,
        uint64 sequenceNumber,
        bytes[] calldata updateData,
        bytes32[] calldata priceIds
    ) external payable override {
        Request storage req = findActiveRequest(sequenceNumber);

        // Check provider exclusivity using configurable period
        if (
            block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds
        ) {
            require(
                providerToCredit == req.provider,
                "Only assigned provider during exclusivity period"
            );
        }

        // Verify priceIds match
        require(
            priceIds.length == req.priceIdPrefixes.length,
            "Price IDs length mismatch"
        );
        for (uint8 i = 0; i < req.priceIdPrefixes.length; i++) {
            // Extract first 8 bytes of the provided price ID
            bytes32 priceId = priceIds[i];
            bytes8 prefix;
            assembly {
                prefix := priceId
            }

            // Compare with stored prefix
            if (prefix != req.priceIdPrefixes[i]) {
                // Now we can directly use the bytes8 prefix in the error
                revert InvalidPriceIds(priceIds[i], req.priceIdPrefixes[i]);
            }
        }

        // TODO: should this use parsePriceFeedUpdatesUnique? also, do we need to add 1 to maxPublishTime?
        IPyth pyth = IPyth(_state.pyth);
        uint256 pythFee = pyth.getUpdateFee(updateData);
        PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{
            value: pythFee
        }(
            updateData,
            priceIds,
            SafeCast.toUint64(req.publishTime),
            SafeCast.toUint64(req.publishTime)
        );

        // TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
        // If executeCallback can revert, then funds can be permanently locked in the contract.
        // TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
        // This should take funds from the expected provider and give to providerToCredit. The penalty should probably scale
        // with time in order to ensure that the callback eventually gets executed.
        // (There may be exploits with ^ though if the consumer contract is malicious ?)
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L381-393)
```text
    function registerProvider(
        uint96 baseFeeInWei,
        uint96 feePerFeedInWei,
        uint96 feePerGasInWei
    ) external override {
        ProviderInfo storage provider = _state.providers[msg.sender];
        require(!provider.isRegistered, "Provider already registered");
        provider.baseFeeInWei = baseFeeInWei;
        provider.feePerFeedInWei = feePerFeedInWei;
        provider.feePerGasInWei = feePerGasInWei;
        provider.isRegistered = true;
        emit ProviderRegistered(msg.sender, feePerGasInWei);
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L846-846)
```text
        uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
```
