### Title
Missing Storage Gaps in Abstract Base Contracts of UUPS Upgradeable Hierarchy — (`EntropyState.sol`, `Entropy.sol`, `EntropyGovernance.sol`, `EchoState.sol`, `SchedulerState.sol`, `PythState.sol`, `Executor.sol`)

---

### Summary

None of the abstract or base contracts that form the storage and logic layers of Pyth's UUPS-upgradeable contracts declare a `__gap` storage reservation. A confirmed `grep` across all `.sol` files returns zero matches for `__gap`. If any of these base contracts ever receives a new top-level state variable in a future upgrade, the storage layout of every child upgradeable contract will shift, silently overwriting live state.

---

### Finding Description

Every Pyth upgradeable contract follows the same pattern: a concrete "state" contract holds a single packed `_state` struct, one or more abstract logic contracts inherit from it, and a final `*Upgradable` / `*Upgradeable` contract ties everything together via UUPS.

**Affected inheritance chains (all confirmed to have zero `__gap` declarations):**

| Upgradeable contract | Base contracts with state |
|---|---|
| `EntropyUpgradable` | `EntropyState` → `Entropy` (abstract) → `EntropyGovernance` (abstract) |
| `EchoUpgradeable` | `EchoState` → `Echo` (abstract) |
| `SchedulerUpgradeable` | `SchedulerState` → `Scheduler` (abstract) → `SchedulerGovernance` (abstract) |
| `PythUpgradable` | `PythState` → `PythGetters` → `PythSetters` → `PythGovernance` (abstract) |
| `ExecutorUpgradable` | `Executor` (5 direct top-level state variables) |

`EntropyState` declares `EntropyInternalStructs.State _state` at slot 0 with no trailing gap: [1](#0-0) 

`Entropy` is abstract, inherits `EntropyState`, and adds no gap: [2](#0-1) 

`EntropyGovernance` is abstract, inherits `EntropyState`, and adds no gap: [3](#0-2) 

`EntropyUpgradable` inherits all three plus OZ upgradeable contracts: [4](#0-3) 

`Executor` has five direct top-level state variables with no gap: [5](#0-4) 

`EchoState` holds the full Echo state struct with no gap: [6](#0-5) 

`SchedulerState` holds the full Scheduler state struct with no gap: [7](#0-6) 

`PythState` holds the full Pyth state struct with no gap: [8](#0-7) 

---

### Impact Explanation

If a developer adds a new top-level state variable to any of these base contracts during a future upgrade (e.g., a new `uint256` directly in `EntropyState` after `_state`, or a new variable in `Entropy`/`EntropyGovernance`), the EVM storage layout of the child upgradeable contract shifts. Variables that were previously at slot N are now read from slot N+k, silently returning wrong values. For `EntropyUpgradable` this means:

- Provider fee balances (`_state.providers[addr].accruedFeesInWei`) could be read from the wrong slot, enabling theft or denial of withdrawal.
- In-flight request commitments (`_state.requests[i].commitment`) could be corrupted, breaking randomness verification.
- Admin and default-provider addresses could be overwritten, enabling unauthorized governance actions.

For `ExecutorUpgradable`, corruption of `lastExecutedSequence` would break replay protection, allowing governance messages to be re-executed.

---

### Likelihood Explanation

The likelihood is **Low** in isolation: it requires a future upgrade that introduces a new top-level variable into one of these base contracts. However, the Pyth contracts are actively developed and upgraded. The absence of any `__gap` across the entire codebase means there is no safety net for any of the five upgradeable contracts. The "single struct" pattern partially mitigates the risk (adding fields inside the struct is safe), but it does not protect against new top-level variables being added outside the struct, which is a common developer mistake.

---

### Recommendation

Add a `uint256[50] private __gap;` (or similar size) at the end of each base/abstract contract that participates in an upgradeable hierarchy:

- `EntropyState`, `Entropy`, `EntropyGovernance`
- `EchoState`, `Echo`
- `SchedulerState`, `Scheduler`, `SchedulerGovernance`
- `PythState`, `PythGetters`, `PythSetters`, `PythGovernance`
- `Executor`

This is the standard OpenZeppelin pattern used by `OwnableUpgradeable`, `Ownable2StepUpgradeable`, and `UUPSUpgradeable` themselves, all of which already include `__gap` arrays.

---

### Proof of Concept

1. Confirm zero `__gap` declarations:
   ```
   grep -r "__gap" target_chains/ethereum/contracts/contracts/ --include="*.sol"
   # → no output
   ```

2. Current storage layout of `EntropyUpgradable` (simplified):
   - Slots 0–49: `Initializable.__gap` (OZ, safe)
   - Slots 50–99: `OwnableUpgradeable` + gap (OZ, safe)
   - Slots 100–149: `UUPSUpgradeable` + gap (OZ, safe)
   - Slot 150: `EntropyState._state` (large struct, no gap after it)

3. Suppose a future upgrade adds `address newVar;` to `EntropyState` before `_state`. All existing storage reads for `_state` now point one slot higher, corrupting every provider balance, request, and admin address in the live contract. [1](#0-0) [9](#0-8)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L69-70)
```text
abstract contract Entropy is IEntropy, EntropyState {
    using ExcessivelySafeCall for address;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyGovernance.sol (L11-11)
```text
abstract contract EntropyGovernance is EntropyState {
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyUpgradable.sol (L1-18)
```text
// SPDX-License-Identifier: Apache 2
pragma solidity ^0.8.0;

import "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import "@openzeppelin/contracts-upgradeable/access/Ownable2StepUpgradeable.sol";
import "@pythnetwork/entropy-sdk-solidity/EntropyErrors.sol";

import "./EntropyGovernance.sol";
import "./Entropy.sol";

contract EntropyUpgradable is
    Initializable,
    Ownable2StepUpgradeable,
    UUPSUpgradeable,
    Entropy,
    EntropyGovernance
{
```

**File:** target_chains/ethereum/contracts/contracts/executor/Executor.sol (L40-45)
```text
    IWormhole private wormhole;
    uint64 private lastExecutedSequence;
    uint16 private chainId;

    uint16 private ownerEmitterChainId;
    bytes32 private ownerEmitterAddress;
```

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L5-70)
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
```

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerState.sol (L8-38)
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

**File:** target_chains/ethereum/contracts/contracts/pyth/PythState.sol (L46-48)
```text
contract PythState {
    PythStorage.State _state;
}
```
