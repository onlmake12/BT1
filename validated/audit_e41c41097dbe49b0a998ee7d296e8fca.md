### Title
Weak On-Chain PRNG in `random()` Used as User Contribution in `requestV2()` Breaks Entropy's Two-Party Security Guarantee — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `Entropy` contract exposes three convenience overloads of `requestV2()` that call an internal `random()` function to generate the user's contribution on-chain. That function derives its output solely from `block.timestamp`, `block.prevrandao`, `msg.sender`, and the publicly readable `_state.seed`. All four inputs are observable or controllable by a block proposer before the transaction is finalized. This makes the user contribution fully predictable to a validator, collapsing the Entropy protocol's two-party security guarantee down to trusting the provider alone.

---

### Finding Description

The `random()` function is defined at the bottom of `Entropy.sol`:

```solidity
function random() internal returns (bytes32) {
    _state.seed = keccak256(
        abi.encodePacked(
            block.timestamp,
            block.prevrandao,
            msg.sender,
            _state.seed
        )
    );
    return _state.seed;
}
``` [1](#0-0) 

It is called in three public entry points that omit the `userContribution` argument:

```solidity
function requestV2() external payable override returns (uint64) {
    assignedSequenceNumber = requestV2(getDefaultProvider(), random(), 0);
}

function requestV2(uint32 gasLimit) external payable override returns (uint64) {
    assignedSequenceNumber = requestV2(getDefaultProvider(), random(), gasLimit);
}

function requestV2(address provider, uint32 gasLimit) external payable override returns (uint64) {
    assignedSequenceNumber = requestV2(provider, random(), gasLimit);
}
``` [2](#0-1) 

The value returned by `random()` is passed directly as `userContribution` to the full `requestV2(address, bytes32, uint32)` overload, which hashes it into the on-chain commitment and later uses the raw value in `combineRandomValues`:

```solidity
combinedRandomness = keccak256(
    abi.encodePacked(userRandomness, providerRandomness, blockHash)
);
``` [3](#0-2) 

Every input to `random()` is known to a block proposer at inclusion time:

| Input | Why it is known |
|---|---|
| `block.timestamp` | Set by the proposer |
| `block.prevrandao` | The RANDAO reveal; known to the proposer before the block is finalized |
| `msg.sender` | Visible in the pending transaction |
| `_state.seed` | Public contract storage; readable from any node |

---

### Impact Explanation

The Entropy protocol's core security property, stated in the contract's own comments, is:

> *"the result is random as long as either the provider or user is honest"* [4](#0-3) 

When a user calls one of the no-argument `requestV2()` overloads, the contract generates the user contribution via `random()`. Because all inputs are observable or controllable by the block proposer, the user contribution is no longer secret. A validator who is also the block proposer can:

1. Read `_state.seed` from chain state.
2. Know `block.timestamp` and `block.prevrandao` (they set/know these values).
3. See `msg.sender` from the pending transaction.
4. Compute the exact `userContribution` that `random()` will produce.
5. Since the provider's hash chain is public, compute the final random number `r = keccak256(userContribution, providerContribution, 0)` before the request is even included in a block.
6. Selectively include or exclude the transaction, or reorder it, to steer the outcome of any application consuming the random number (NFT trait assignment, game outcomes, lottery winner selection, etc.).

This fully breaks the two-party guarantee for all callers of the convenience `requestV2()` overloads, reducing security to trusting the provider alone — the weaker, single-party model.

---

### Likelihood Explanation

- The three affected `requestV2()` overloads are the **recommended, documented entry points** for Entropy consumers (the v2 API guide explicitly shows `entropy.requestV2{value: fee}()` with no arguments).
- Any validator who is a block proposer on the target chain can execute this attack with no special access, no leaked keys, and no off-chain infrastructure beyond reading public chain state.
- On chains with a small validator set or where the Pyth default provider is also a validator, the attack surface is even larger.
- The `_state.seed` is updated only when `random()` is called, so between calls it is static and trivially readable.

---

### Recommendation

Replace the on-chain PRNG with a caller-supplied contribution, and require callers to commit off-chain before calling `requestV2()`. The existing `requestV2(address provider, bytes32 userContribution, uint32 gasLimit)` overload already supports this correctly. The convenience overloads should either be removed or replaced with a version that accepts a user-supplied `userContribution`:

```solidity
// Remove or deprecate:
function requestV2() external payable override returns (uint64) { ... }
function requestV2(uint32 gasLimit) external payable override returns (uint64) { ... }
function requestV2(address provider, uint32 gasLimit) external payable override returns (uint64) { ... }
```

If a no-argument convenience overload must be kept, document clearly that it provides **no user-side entropy** and that security relies entirely on the provider. Alternatively, require the caller to supply a commitment hash derived from a secret generated off-chain, consistent with the protocol's original design.

---

### Proof of Concept

```solidity
// Attacker is a validator / block proposer on the target chain.
// Before including the victim's requestV2() transaction:

// 1. Read current seed from contract storage (slot for _state.seed).
bytes32 currentSeed = /* read _state.seed via eth_getStorageAt */;

// 2. Compute the user contribution that random() will produce.
bytes32 predictedUserContribution = keccak256(
    abi.encodePacked(
        block.timestamp,   // proposer sets this
        block.prevrandao,  // known to proposer before block finalization
        victimAddress,     // from pending tx
        currentSeed
    )
);

// 3. Retrieve provider's current revealed value (public on-chain).
bytes32 providerContribution = /* provider's x_i from hash chain */;

// 4. Compute the final random number before the request is committed.
bytes32 predictedRandom = keccak256(
    abi.encodePacked(
        predictedUserContribution,
        providerContribution,
        bytes32(0) // useBlockhash is false for requestV2
    )
);

// 5. If predictedRandom is unfavorable (e.g., victim wins a lottery),
//    exclude the transaction from this block and try the next block.
//    If favorable (e.g., attacker wins), include it.
```

The `random()` function and the three affected `requestV2()` overloads are the necessary vulnerable steps; no privileged access is required beyond being a block proposer. [1](#0-0) [2](#0-1)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L50-54)
```text
//
// This protocol has the same security properties as the 2-party randomness protocol above: as long as either
// the provider or user is honest, the number r is random. Note that this analysis assumes that
// providers cannot frontrun user transactions -- a dishonest provider who frontruns user transaction can
// manipulate the result.
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L286-310)
```text
    function requestV2()
        external
        payable
        override
        returns (uint64 assignedSequenceNumber)
    {
        assignedSequenceNumber = requestV2(getDefaultProvider(), random(), 0);
    }

    function requestV2(
        uint32 gasLimit
    ) external payable override returns (uint64 assignedSequenceNumber) {
        assignedSequenceNumber = requestV2(
            getDefaultProvider(),
            random(),
            gasLimit
        );
    }

    function requestV2(
        address provider,
        uint32 gasLimit
    ) external payable override returns (uint64 assignedSequenceNumber) {
        assignedSequenceNumber = requestV2(provider, random(), gasLimit);
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L950-958)
```text
    function combineRandomValues(
        bytes32 userRandomness,
        bytes32 providerRandomness,
        bytes32 blockHash
    ) public pure override returns (bytes32 combinedRandomness) {
        combinedRandomness = keccak256(
            abi.encodePacked(userRandomness, providerRandomness, blockHash)
        );
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L1079-1089)
```text
    function random() internal returns (bytes32) {
        _state.seed = keccak256(
            abi.encodePacked(
                block.timestamp,
                block.prevrandao,
                msg.sender,
                _state.seed
            )
        );
        return _state.seed;
    }
```
