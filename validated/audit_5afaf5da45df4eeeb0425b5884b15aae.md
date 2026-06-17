### Title
Predictable On-Chain PRNG in `random()` Allows Provider to Pre-Compute User Contribution and Manipulate Entropy Outcomes - (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `random()` internal function in `Entropy.sol` generates the user's contribution to the commit-reveal protocol using entirely predictable, publicly observable on-chain values (`block.timestamp`, `block.prevrandao`, `msg.sender`, `_state.seed`). Three `requestV2` overloads call this function as the user's entropy contribution. Because all inputs are observable before block finalization, the provider can compute the user's contribution from the mempool, derive the final random number in advance, and selectively withhold revelation — breaking the core security guarantee of the Entropy protocol.

---

### Finding Description

The `random()` function at the bottom of `Entropy.sol` is:

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

This output is used as the `userContribution` (i.e., `x_U`) in three `requestV2` overloads:

```solidity
function requestV2() external payable override returns (uint64 assignedSequenceNumber) {
    assignedSequenceNumber = requestV2(getDefaultProvider(), random(), 0);
}

function requestV2(uint32 gasLimit) external payable override returns (uint64 assignedSequenceNumber) {
    assignedSequenceNumber = requestV2(getDefaultProvider(), random(), gasLimit);
}

function requestV2(address provider, uint32 gasLimit) external payable override returns (uint64 assignedSequenceNumber) {
    assignedSequenceNumber = requestV2(provider, random(), gasLimit);
}
``` [2](#0-1) 

All four inputs to `random()` are predictable or publicly observable **before** the transaction is included in a block:

| Input | Why Predictable |
|---|---|
| `block.timestamp` | Known to validators; estimable by anyone within the ~12s slot window |
| `block.prevrandao` | The RANDAO value from the **previous** block — fully public before the current block is built |
| `msg.sender` | Visible in the pending transaction in the mempool |
| `_state.seed` | A storage variable in `EntropyState.sol`; readable by anyone via `eth_getStorageAt` | [3](#0-2) 

The Entropy protocol's security model requires that `x_U` be **unknown to the provider** at the time the provider committed to their hash chain. The final random number is:

```
r = keccak256(x_U, x_i, blockHash)
```

where `useBlockhash = false` for all `requestV2` callback variants, so `blockHash = bytes32(0)`. [4](#0-3) [5](#0-4) 

Since the provider knows `x_i` (it is their own hash chain value) and can compute `x_U` from the observable inputs, they can compute `r` before deciding whether to reveal `x_i`. This enables a selective-reveal (censorship) attack.

The `IEntropyV2` interface documentation acknowledges a weaker version of this risk — "a dishonest validator and provider can collude" — but the actual vulnerability is worse: **the provider alone**, without any validator collusion, can predict `x_U` by observing the mempool and reading `_state.seed` from chain state. [6](#0-5) 

---

### Impact Explanation

Any user calling `requestV2()`, `requestV2(uint32)`, or `requestV2(address, uint32)` — the three variants that use `random()` for user contribution — has their randomness outcome fully computable by the provider before the provider reveals. The provider can:

1. Compute `x_U` by simulating the pending transaction.
2. Compute `r = keccak256(x_U, x_i, 0)` using their known `x_i`.
3. If `r` is unfavorable (e.g., user wins a lottery, NFT mint produces a rare trait), withhold revelation indefinitely.
4. If `r` is favorable to the provider, reveal normally.

This breaks the fundamental security guarantee of the Entropy protocol: **"the result is random as long as either A or B are honest."** With a predictable `x_U`, the provider is never truly "blind" to the outcome, so the protocol's honesty assumption for the user side is nullified.

Impact: complete manipulation of randomness outcomes for all users of the no-argument `requestV2` family. Applications built on these variants (lotteries, NFT mints, games) are fully exploitable by the provider.

---

### Likelihood Explanation

- The provider role is **permissionless** — anyone can register as a provider via `register()`.
- The attack requires only: reading `_state.seed` via `eth_getStorageAt` (trivial), observing the mempool (standard), and simulating a transaction (standard).
- No validator collusion, no privileged access, no leaked keys required.
- The three vulnerable `requestV2` overloads are the **recommended default API** (zero-argument variant is the simplest integration path), making widespread use likely.

Likelihood: **High** for any provider who wishes to manipulate outcomes.

---

### Recommendation

1. **Remove or deprecate** the three `requestV2` overloads that call `random()` internally, or add prominent on-chain warnings (e.g., a `require` with a message) directing users to the `requestV2(address, bytes32, uint32)` variant.
2. If an on-chain PRNG is retained for UX convenience, replace `block.timestamp` and `block.prevrandao` with a value that is not known before the transaction is submitted — for example, a commitment to a future block hash. However, note that no purely on-chain PRNG can be made fully secure against a colluding validator+provider.
3. The safest path is to require callers to supply their own off-chain-generated `userRandomNumber`, as the `requestV2(address, bytes32, uint32)` variant already supports. [7](#0-6) 

---

### Proof of Concept

**Setup**: Attacker registers as a provider with a known hash chain `[x_{N-1}, ..., x_1, x_0]`.

**Attack steps**:

1. Victim calls `requestV2()` (no-arg), paying the fee. Transaction enters the mempool.

2. Attacker (provider) reads from chain:
   - `_state.seed` via `eth_getStorageAt(entropyContract, seedSlot)`
   - `block.prevrandao` from the latest finalized block header
   - `msg.sender` from the pending transaction
   - `block.timestamp` (estimable as current slot timestamp)

3. Attacker simulates:
   ```solidity
   bytes32 x_U = keccak256(abi.encodePacked(block.timestamp, block.prevrandao, victim, _state.seed));
   ```

4. Attacker computes the assigned sequence number `i` (it equals `providerInfo.sequenceNumber` at time of request, publicly readable), retrieves `x_i` from their own hash chain.

5. Attacker computes:
   ```solidity
   bytes32 r = keccak256(abi.encodePacked(x_U, x_i, bytes32(0)));
   ```

6. If `r` is unfavorable (e.g., victim wins the lottery: `uint256(r) % 2 == 0`), attacker withholds revelation. Victim's request is never fulfilled. Attacker repeats for the next request with a different `x_i` until a favorable `r` is found.

7. If `r` is favorable, attacker reveals `x_i` normally, collecting the fee. [1](#0-0) [8](#0-7)

### Citations

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L363-372)
```text
        EntropyStructsV2.Request storage req = requestHelper(
            provider,
            constructUserCommitment(userContribution),
            // If useBlockHash is set to true, it allows a scenario in which the provider and miner can collude.
            // If we remove the blockHash from this, the provider would have no choice but to provide its committed
            // random number. Hence, useBlockHash is set to false.
            false,
            true,
            gasLimit
        );
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

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyState.sol (L40-42)
```text
        // Seed for in-contract PRNG. This seed is used to generate user random numbers in some callback flows.
        bytes32 seed;
    }
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L21-25)
```text
    /// Note that this method uses an in-contract PRNG to generate the user's contribution to the random number.
    /// This approach modifies the security guarantees such that a dishonest validator and provider can
    /// collude to manipulate the result (as opposed to a malicious user and provider). That is, the user
    /// now trusts the validator honestly draw a random number. If you wish to avoid this trust assumption,
    /// call a variant of `requestV2` that accepts a `userRandomNumber` parameter.
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L97-101)
```text
    function requestV2(
        address provider,
        bytes32 userRandomNumber,
        uint32 gasLimit
    ) external payable returns (uint64 assignedSequenceNumber);
```
