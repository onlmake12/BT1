### Title
Missing Storage Gaps in Upgradeable Base Contracts Across All Pyth EVM Protocol Contracts - (File: `target_chains/ethereum/contracts/contracts/entropy/EntropyState.sol`, `Entropy.sol`, `EntropyGovernance.sol`)

---

### Summary

Every upgradeable EVM contract in the Pyth protocol (`EntropyUpgradable`, `SchedulerUpgradeable`, `EchoUpgradeable`, `ExecutorUpgradable`, `PythUpgradable`) inherits from one or more base contracts that define storage variables but declare no `__gap` reserve. Adding any new storage variable to any of these base contracts in a future upgrade will silently shift the storage layout of the inheriting upgradeable proxy, corrupting all existing state.

---

### Finding Description

The OpenZeppelin upgradeable contract pattern requires that every base contract in an inheritance chain reserve a `__gap` array so that future storage additions to that base contract do not collide with storage slots already occupied by derived contracts.

None of the Pyth base contracts do this. The affected inheritance chains are:

**Entropy (most critical — live production contract with real funds):**
- `EntropyState` declares `_state` at its first slot with no trailing `__gap`.
- `Entropy` inherits `EntropyState`, adds no `__gap`.
- `EntropyGovernance` inherits `EntropyState`, adds no `__gap`.
- `EntropyUpgradable` inherits both `Entropy` and `EntropyGovernance`. [1](#0-0) 

**Scheduler:**
- `SchedulerState` declares `_state` with no `__gap`.
- `Scheduler` and `SchedulerGovernance` both inherit `SchedulerState` with no `__gap`.
- `SchedulerUpgradeable` inherits both. [2](#0-1) 

**Echo:**
- `EchoState` declares `_state` with no `__gap`.
- `Echo` inherits `EchoState` with no `__gap`.
- `EchoUpgradeable` inherits `Echo`. [3](#0-2) 

**Executor:**
- `Executor` declares five raw storage variables (`wormhole`, `lastExecutedSequence`, `chainId`, `ownerEmitterChainId`, `ownerEmitterAddress`) with no `__gap`.
- `ExecutorUpgradable` inherits `Executor`. [4](#0-3) 

**Pyth core:**
- `PythState` declares `_state` with no `__gap`.
- `Pyth` (via `PythGetters`/`PythSetters`) and `PythGovernance` inherit it with no `__gap`.
- `PythUpgradable` inherits both. [5](#0-4) 

---

### Impact Explanation

If any future upgrade adds a new storage variable to a base contract (e.g., a new field in `EntropyInternalStructs.State`, or a new variable in `EntropyGovernance`), the EVM storage layout of the deployed proxy shifts. Every subsequent storage read/write in the upgraded implementation will read from the wrong slot. For `EntropyUpgradable` this means:

- Provider fee balances (`accruedFeesInWei`) could be read from a slot that now holds a different value, enabling fee theft or denial of withdrawal.
- In-flight request commitments could be corrupted, breaking randomness fulfillment.
- The `admin` address could be overwritten, locking governance.

The same class of corruption applies to `SchedulerUpgradeable` (subscription balances), `ExecutorUpgradable` (governance sequence replay protection), and `PythUpgradable` (price feed data). [6](#0-5) 

---

### Likelihood Explanation

The Pyth protocol actively upgrades its contracts. Any developer adding a new configuration field, a new counter, or a new flag to `EntropyState`, `Entropy`, or `EntropyGovernance` — a routine operation — will silently introduce a storage collision. There is no compiler warning, no test that catches this by default, and no `__gap` to absorb the addition. The probability of a future storage-extending upgrade is high given the active development pace visible in the repository. [7](#0-6) 

---

### Recommendation

Add a `uint256[N] private __gap;` at the end of every base contract in each upgradeable inheritance chain, where `N` is chosen to bring the total reserved slots to a round number (e.g., 50). Affected contracts:

- `EntropyState`, `Entropy`, `EntropyGovernance`
- `SchedulerState`, `Scheduler`, `SchedulerGovernance`
- `EchoState`, `Echo`
- `Executor`
- `PythState`, `PythGetters`, `PythSetters`, `PythGovernance`

Follow the [OpenZeppelin storage gap guidance](https://docs.openzeppelin.com/contracts/4.x/upgradeable#storage_gaps).

---

### Proof of Concept

**Scenario (Entropy):**

1. Current deployment: `EntropyUpgradable` proxy points to implementation V1. `EntropyState._state` occupies slots 0–N. `OwnableUpgradeable` occupies slots above that.

2. Developer adds one new field to `EntropyInternalStructs.State` (e.g., `uint128 newFeeType`). This is a routine change.

3. New implementation V2 is deployed and the proxy is upgraded via `EntropyUpgradable.upgradeTo()`.

4. All existing storage slots shift by one 32-byte word. The slot that previously held `_state.admin` now holds `_state.pythFeeInWei`. The slot that held `_state.providers[addr].accruedFeesInWei` now holds a different provider's data.

5. An Entropy provider calls `withdraw(amount)`:
   ```solidity
   // EntropyState.sol line 50: _state is at slot 0
   // After upgrade, providerInfo.accruedFeesInWei reads from wrong slot
   require(providerInfo.accruedFeesInWei >= amount, "Insufficient balance");
   providerInfo.accruedFeesInWei -= amount;
   (bool sent, ) = msg.sender.call{value: amount}("");
   ```
   The corrupted `accruedFeesInWei` value could be astronomically large (reading from a hash-table slot), allowing the provider to drain the contract, or zero, permanently locking all provider fees. [8](#0-7) [1](#0-0)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyState.sol (L45-51)
```text
contract EntropyState {
    // The size of the requests hash table. Must be a power of 2.
    uint8 public constant NUM_REQUESTS = 32;
    bytes1 public constant NUM_REQUESTS_MASK = 0x1f;

    EntropyInternalStructs.State _state;
}
```

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerState.sol (L8-39)
```text
contract SchedulerState {
    struct State {
        /// Monotonically increasing counter for subscription IDs
        uint256 subscriptionNumber;
        /// Pyth contract for parsing updates and verifying sigs & timestamps
        address pyth;
        /// Admin address for governance actions
        address admin;
        // proposedAdmin is the new admin's account address proposed by either the owner or the current admin.
        // If there is no pending transfer request, this value will hold `address(0)`.
        address proposedAdmin;
        /// Fee in wei charged to subscribers per single update triggered by a keeper
        uint128 singleUpdateKeeperFeeInWei;
        /// Minimum balance required per price feed in a subscription
        uint128 minimumBalancePerFeed;
        /// Sub ID -> subscription parameters (which price feeds, when to update, etc)
        mapping(uint256 => SchedulerStructs.SubscriptionParams) subscriptionParams;
        /// Sub ID -> subscription status (metadata about their sub)
        mapping(uint256 => SchedulerStructs.SubscriptionStatus) subscriptionStatuses;
        /// Sub ID -> price ID -> latest parsed price update for the subscribed feed
        mapping(uint256 => mapping(bytes32 => PythStructs.PriceFeed)) priceUpdates;
        /// Sub ID -> manager address
        mapping(uint256 => address) subscriptionManager;
        /// Array of active subscription IDs.
        /// Gas optimization to avoid scanning through all subscriptions when querying for all active ones.
        uint256[] activeSubscriptionIds;
        /// Sub ID -> index in activeSubscriptionIds array + 1 (0 means not in array).
        /// This lets us avoid a linear scan of `activeSubscriptionIds` when deactivating a subscription.
        mapping(uint256 => uint256) activeSubscriptionIndex;
    }
    State internal _state;

```

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L5-71)
```text
contract EchoState {
    uint8 public constant NUM_REQUESTS = 32;
    bytes1 public constant NUM_REQUESTS_MASK = 0x1f;
    // Maximum number of price feeds per request. This limit keeps gas costs predictable and reasonable. 10 is a reasonable number for most use cases.
    // Requests with more than 10 price feeds should be split into multiple requests
    uint8 public constant MAX_PRICE_IDS = 10;

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

    struct ProviderInfo {
        // Slot 1: 12 + 12 + 8 = 32 bytes
        uint96 baseFeeInWei;
        uint96 feePerFeedInWei;
        // 8 bytes padding

        // Slot 2: 12 + 16 + 4 = 32 bytes
        uint96 feePerGasInWei;
        uint128 accruedFeesInWei;
        // 4 bytes padding

        // Slot 3: 20 + 1 + 11 = 32 bytes
        address feeManager;
        bool isRegistered;
        // 11 bytes padding
    }

    struct State {
        // Slot 1: 20 + 4 + 8 = 32 bytes
        address admin;
        uint32 exclusivityPeriodSeconds;
        uint64 currentSequenceNumber;
        // Slot 2: 20 + 8 + 4 = 32 bytes
        address pyth;
        uint64 firstUnfulfilledSeq;
        // 4 bytes padding

        // Slot 3: 20 + 12 = 32 bytes
        address defaultProvider;
        uint96 pythFeeInWei;
        // Slot 4: 16 + 16 = 32 bytes
        uint128 accruedFeesInWei;
        // 16 bytes padding

        // These take their own slots regardless of ordering
        Request[NUM_REQUESTS] requests;
        mapping(bytes32 => Request) requestsOverflow;
        mapping(address => ProviderInfo) providers;
    }
    State internal _state;
}
```

**File:** target_chains/ethereum/contracts/contracts/executor/Executor.sol (L40-46)
```text
    IWormhole private wormhole;
    uint64 private lastExecutedSequence;
    uint16 private chainId;

    uint16 private ownerEmitterChainId;
    bytes32 private ownerEmitterAddress;

```

**File:** target_chains/ethereum/contracts/contracts/pyth/PythState.sol (L46-48)
```text
contract PythState {
    PythStorage.State _state;
}
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyUpgradable.sol (L12-18)
```text
contract EntropyUpgradable is
    Initializable,
    Ownable2StepUpgradeable,
    UUPSUpgradeable,
    Entropy,
    EntropyGovernance
{
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyGovernance.sol (L11-12)
```text
abstract contract EntropyGovernance is EntropyState {
    event PythFeeSet(uint oldPythFee, uint newPythFee);
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L150-163)
```text
    function withdraw(uint128 amount) public override {
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            msg.sender
        ];

        // Use checks-effects-interactions pattern to prevent reentrancy attacks.
        require(
            providerInfo.accruedFeesInWei >= amount,
            "Insufficient balance"
        );
        providerInfo.accruedFeesInWei -= amount;

        // Interaction with an external contract or token transfer
        (bool sent, ) = msg.sender.call{value: amount}("");
```
