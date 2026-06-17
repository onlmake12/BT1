### Title
Weak On-Chain PRNG Seeded With Manipulable Block Values Allows Validator-Provider Collusion to Bias Entropy Results - (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `random()` function in `Entropy.sol` generates the user's contribution to the commit-reveal protocol using only `block.prevrandao`, `block.timestamp`, `msg.sender`, and a publicly readable on-chain seed. All of these inputs are known to or directly controlled by the current block proposer (validator). Three `requestV2()` overloads — the default, most-used API surface — silently substitute this weak PRNG output for the user's secret contribution, eliminating the user's honest-party guarantee and enabling a colluding validator+provider to predict and bias the final random number.

---

### Finding Description

The `random()` internal function at line 1079 of `Entropy.sol` is:

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

This output is used as the `userContribution` in three `requestV2()` overloads — the default API that most integrators use:

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

Every input to `random()` is either public or controlled by the block proposer:

| Input | Observability |
|---|---|
| `block.prevrandao` | Known to the current block proposer before block finalization |
| `block.timestamp` | Set by the current block proposer |
| `msg.sender` | Visible in the mempool |
| `_state.seed` | Public contract storage; initialized to `bytes32(0)` | [3](#0-2) 

The Entropy commit-reveal protocol's security guarantee is: `r = hash(x_i, x_U)` is unbiased as long as **either** the user or the provider is honest. When `random()` replaces the user's secret contribution, the user's honesty guarantee is eliminated. The provider now only needs to collude with the block proposer to know `x_U` before deciding whether to reveal `x_i`.

---

### Impact Explanation

A colluding validator (block proposer) and provider can:

1. Read `_state.seed` from on-chain storage.
2. Know `block.prevrandao` (they set it), `block.timestamp` (they set it), and `msg.sender` (from the mempool).
3. Compute the exact value `x_U` that `random()` will produce for any pending `requestV2()` call.
4. Compute `r = hash(x_i, x_U)` using the provider's known hash chain value `x_i`.
5. If `r` is unfavorable (e.g., a lottery user wins), reorder transactions, adjust `block.timestamp`, or withhold the reveal to steer toward a favorable outcome.

All downstream applications using the default `requestV2()` — games, lotteries, NFT mints, DeFi randomness — receive a manipulable random number instead of a secure one. [4](#0-3) 

---

### Likelihood Explanation

The default provider is a known, identifiable entity (Pyth/Fortuna). On PoS chains, block proposers are also known entities. The attack is economically rational whenever the value of the randomness outcome (e.g., a high-stakes lottery jackpot, a rare NFT trait) exceeds the cost of coordination. The `IEntropyV2` interface documentation itself acknowledges this exact attack vector:

> "This approach modifies the security guarantees such that a dishonest validator and provider can collude to manipulate the result." [5](#0-4) 

The fact that the protocol documentation acknowledges the risk confirms the root cause is real and reachable, not theoretical.

---

### Recommendation

**Short term**: Deprecate or clearly gate the three `requestV2()` overloads that call `random()` internally. Require callers to supply their own `userRandomNumber` (the fourth overload `requestV2(address, bytes32, uint32)` is safe). Add a prominent on-chain revert or warning for the weak-PRNG variants.

**Long term**: Remove the on-chain PRNG path entirely. The security model of Entropy is predicated on the user providing an independent secret contribution. Generating that contribution from manipulable block variables defeats the protocol's core guarantee. If a no-argument convenience API is desired, document that it provides **no randomness security** and should only be used for non-security-sensitive applications.

---

### Proof of Concept

1. Alice's contract calls `entropy.requestV2{value: fee}()` (no arguments — the default integration pattern).
2. The contract internally calls `random()`, computing `x_U = keccak256(block.timestamp, block.prevrandao, msg.sender, _state.seed)`.
3. Eve is the current block proposer and is colluding with the Fortuna provider. She knows `block.prevrandao` (she computed it), `block.timestamp` (she set it), `msg.sender` (from the mempool), and `_state.seed` (read from chain).
4. Eve computes `x_U` off-chain, then computes `r = keccak256(x_i, x_U)` using the provider's known next hash chain value `x_i`.
5. If `r` maps to an outcome unfavorable to Eve (e.g., Alice wins the jackpot), Eve reorders Alice's transaction to a later slot where `_state.seed` has a different value, or adjusts `block.timestamp` by 1 second, producing a different `x_U` and thus a different `r`.
6. Eve repeats until `r` maps to a favorable outcome, then includes Alice's transaction in that block.
7. The provider reveals `x_i`; the contract computes `r` and delivers the biased result to Alice's callback. [1](#0-0) [6](#0-5)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyState.sol (L40-41)
```text
        // Seed for in-contract PRNG. This seed is used to generate user random numbers in some callback flows.
        bytes32 seed;
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L21-25)
```text
    /// Note that this method uses an in-contract PRNG to generate the user's contribution to the random number.
    /// This approach modifies the security guarantees such that a dishonest validator and provider can
    /// collude to manipulate the result (as opposed to a malicious user and provider). That is, the user
    /// now trusts the validator honestly draw a random number. If you wish to avoid this trust assumption,
    /// call a variant of `requestV2` that accepts a `userRandomNumber` parameter.
```
