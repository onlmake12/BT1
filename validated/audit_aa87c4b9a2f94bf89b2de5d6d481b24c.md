### Title
Incomplete Event Emission in `Echo::withdrawAsFeeManager` Omits Provider Address — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary
The `withdrawAsFeeManager` function in `Echo.sol` emits `FeesWithdrawn(msg.sender, amount)`, recording only the fee manager's address as the `recipient`. The `provider` address — whose accrued balance is actually debited — is never included in the event. Off-chain services (e.g., the Fortuna keeper, indexers, dashboards) cannot determine which provider's fees were withdrawn, breaking per-provider fee accounting. The sibling contract `Entropy.sol` correctly emits both `provider` and `msg.sender` in its equivalent function, confirming this is an unintentional omission in Echo.

---

### Finding Description

`Echo.sol` `withdrawAsFeeManager` (lines 360–379):

```solidity
function withdrawAsFeeManager(
    address provider,
    uint128 amount
) external override {
    require(
        msg.sender == _state.providers[provider].feeManager,
        "Only fee manager"
    );
    require(
        _state.providers[provider].accruedFeesInWei >= amount,
        "Insufficient balance"
    );
    _state.providers[provider].accruedFeesInWei -= amount;

    (bool sent, ) = msg.sender.call{value: amount}("");
    require(sent, "Failed to send fees");

    emit FeesWithdrawn(msg.sender, amount);   // ← provider omitted
}
``` [1](#0-0) 

The `FeesWithdrawn` event is defined as:

```solidity
event FeesWithdrawn(address indexed recipient, uint128 amount);
``` [2](#0-1) 

`msg.sender` here is the **fee manager**, not the provider. The event records the fee manager as `recipient` and the `amount`, but never records `provider` — the address whose `accruedFeesInWei` was decremented. Any listener filtering `FeesWithdrawn` events cannot reconstruct which provider's balance was reduced.

**Contrast with `Entropy.sol`'s correct implementation** of the same function (lines 202–208):

```solidity
emit EntropyEvents.Withdrawal(provider, msg.sender, amount);
emit EntropyEventsV2.Withdrawal(provider, msg.sender, amount, bytes(""));
``` [3](#0-2) 

`Entropy.sol` emits both `provider` and `msg.sender` (fee manager). `Echo.sol` emits only `msg.sender`.

**Secondary instance — `registerProvider` (lines 381–393):**

```solidity
provider.baseFeeInWei = baseFeeInWei;
provider.feePerFeedInWei = feePerFeedInWei;
provider.feePerGasInWei = feePerGasInWei;
provider.isRegistered = true;
emit ProviderRegistered(msg.sender, feePerGasInWei);  // baseFeeInWei and feePerFeedInWei omitted
``` [4](#0-3) 

The event `ProviderRegistered(address indexed provider, uint96 feeInWei)` captures only `feePerGasInWei` under the ambiguous name `feeInWei`, silently dropping `baseFeeInWei` and `feePerFeedInWei`. The `ProviderFeeUpdated` event (emitted by `setProviderFee`) correctly includes all three fee fields, making the omission in `ProviderRegistered` inconsistent and misleading. [5](#0-4) 

---

### Impact Explanation

Off-chain services that index `FeesWithdrawn` events to track per-provider fee flows receive an incomplete picture: they see the fee manager address and the amount, but cannot attribute the withdrawal to any specific provider. This breaks:

- **Fee accounting dashboards** that display per-provider accrued/withdrawn balances.
- **Keeper/relayer logic** (e.g., Fortuna) that may use event logs to decide when to top up or monitor provider balances.
- **Audit trails**: a forensic review of on-chain events cannot reconstruct which provider's balance was drained by a given fee manager.

For `registerProvider`, off-chain services reconstructing a provider's fee schedule from events will see only `feePerGasInWei` and silently miss `baseFeeInWei` and `feePerFeedInWei`, leading to incorrect fee estimates for users querying historical state.

---

### Likelihood Explanation

`withdrawAsFeeManager` is callable by any address that has been designated as a fee manager for a registered provider — an unprivileged, permissionless role. Every legitimate fee withdrawal via this path produces a misleading event. The function is part of the live Echo contract, so the defect fires on every real withdrawal.

`registerProvider` is callable by any address wishing to register as an Echo provider — also fully permissionless. Every provider registration emits an incomplete event.

---

### Recommendation

**`withdrawAsFeeManager`:** Add `provider` to the `FeesWithdrawn` event (or emit a separate, richer event), mirroring `Entropy.sol`:

```solidity
// Option A: extend existing event
event FeesWithdrawn(address indexed provider, address indexed recipient, uint128 amount);

// In withdrawAsFeeManager:
emit FeesWithdrawn(provider, msg.sender, amount);
```

**`registerProvider`:** Extend `ProviderRegistered` to include all three fee fields, consistent with `ProviderFeeUpdated`:

```solidity
event ProviderRegistered(
    address indexed provider,
    uint96 baseFeeInWei,
    uint96 feePerFeedInWei,
    uint96 feePerGasInWei
);
```

---

### Proof of Concept

1. Deploy `EchoUpgradeable` with a registered provider `P` and fee manager `FM`.
2. Call `withdrawAsFeeManager(P, 1 ether)` from `FM`.
3. Observe the emitted `FeesWithdrawn` log: `recipient = FM`, `amount = 1 ether`. The address `P` does not appear anywhere in the log.
4. An indexer filtering `FeesWithdrawn` events cannot determine that `P`'s balance was reduced; it only knows `FM` received funds.
5. Repeat for `registerProvider(1e9, 2e9, 3e9)`: the emitted `ProviderRegistered` log shows only `feeInWei = 3e9` (`feePerGasInWei`); `baseFeeInWei = 1e9` and `feePerFeedInWei = 2e9` are invisible to any event listener. [6](#0-5) [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L360-393)
```text
    function withdrawAsFeeManager(
        address provider,
        uint128 amount
    ) external override {
        require(
            msg.sender == _state.providers[provider].feeManager,
            "Only fee manager"
        );
        require(
            _state.providers[provider].accruedFeesInWei >= amount,
            "Insufficient balance"
        );

        _state.providers[provider].accruedFeesInWei -= amount;

        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "Failed to send fees");

        emit FeesWithdrawn(msg.sender, amount);
    }

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

**File:** target_chains/ethereum/contracts/contracts/echo/EchoEvents.sol (L19-44)
```text
    event FeesWithdrawn(address indexed recipient, uint128 amount);

    event PriceUpdateCallbackFailed(
        uint64 indexed sequenceNumber,
        address indexed provider,
        bytes32[] priceIds,
        address requester,
        string reason
    );

    event FeeManagerUpdated(
        address indexed admin,
        address oldFeeManager,
        address newFeeManager
    );

    event ProviderRegistered(address indexed provider, uint96 feeInWei);
    event ProviderFeeUpdated(
        address indexed provider,
        uint96 oldBaseFee,
        uint96 oldFeePerFeed,
        uint96 oldFeePerGas,
        uint96 newBaseFee,
        uint96 newFeePerFeed,
        uint96 newFeePerGas
    );
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L202-208)
```text
        emit EntropyEvents.Withdrawal(provider, msg.sender, amount);
        emit EntropyEventsV2.Withdrawal(
            provider,
            msg.sender,
            amount,
            bytes("")
        );
```
