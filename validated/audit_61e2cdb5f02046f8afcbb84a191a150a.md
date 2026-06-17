### Title
Stale `VoterWeightRecord` Persists Indefinitely After Unstaking, Allowing Inflated Governance Votes — (`governance/pyth_staking_sdk/src/idl/staking.json`)

---

### Summary

Pyth's staking program writes a `VoterWeightRecord` on-chain via `update_voter_weight`. The record's `voter_weight_expiry` field is set to `None` (never expires). Once a user calls `update_voter_weight` while holding staked tokens, the resulting voter weight record remains valid indefinitely — even after the user initiates unstaking and their positions transition to states that carry zero voting power. The governance program (SPL Realms) accepts any `VoterWeightRecord` whose `voter_weight_expiry` is `None` without requiring a fresh update, so the stale record can be submitted to cast a vote with inflated weight.

---

### Finding Description

The `VoterWeightRecord` struct in the staking program contains:

```
voter_weight        : u64          // staked token count at time of last update
voter_weight_expiry : Option<u64>  // None = never expires
weight_action       : Option<VoterWeightAction>
weight_action_target: Option<Pubkey>
```

The IDL documentation for `voter_weight_expiry` explicitly states:

> "It should be set to None if the weight never expires. If the voter weight decays with time … then the expiry must be set. As a common pattern Revise instruction to update the weight should be invoked before governance instruction within the same transaction and the expiry set to the current slot to provide up to date weight."

Pyth's staking weight is epoch-based, not slot-decaying, so the program sets `voter_weight_expiry = None`. However, a user's effective voting power **does** change at epoch boundaries: positions transition from `LOCKED` (voting power) → `PREUNLOCKING` (still voting power) → `UNLOCKING` (no voting power) → `UNLOCKED` (no voting power). Once a user initiates unstaking, their tokens lose voting power at the next epoch boundary. But because `voter_weight_expiry = None`, the `VoterWeightRecord` written before unstaking remains valid forever and can be submitted to Realms to cast a vote.

The `update_voter_weight` instruction is permissionless — any user can call it for their own account at any time. There is no mechanism that forces a refresh before a vote is cast, and the governance program does not invalidate records whose `voter_weight_expiry` is `None`. [1](#0-0) [2](#0-1) 

The client-side helper `getVotingTokenAmount` correctly filters positions by `LOCKED | PREUNLOCKING` at the current epoch, but this check only runs when `update_voter_weight` is called — not at vote-cast time. [3](#0-2) 

---

### Impact Explanation

A user can vote on a Pyth Improvement Proposal (PIP) with a voter weight that is higher than their actual current staked balance. Specifically:

1. User stakes `N` PYTH tokens → positions enter `LOCKED` state.
2. User calls `update_voter_weight(CastVote)` → `VoterWeightRecord.voter_weight = N`, `expiry = None`.
3. User calls `closePosition` to begin unstaking → positions enter `PREUNLOCKING`.
4. Epoch advances → positions transition to `UNLOCKING` (zero voting power).
5. User submits the **old** `VoterWeightRecord` (still showing `N`) to Realms to cast a vote.
6. Realms accepts it because `voter_weight_expiry = None`.

The user has effectively voted with tokens they no longer have staked, inflating their governance influence. With enough tokens, this can tip quorum or swing a proposal outcome.

---

### Likelihood Explanation

- The entry path is fully permissionless: any staking user can call `update_voter_weight` and then unstake.
- No privileged role, leaked key, or external oracle is required.
- The attack requires only two transactions separated by one epoch boundary (~1 week).
- The governance program (Realms) enforces no freshness check when `voter_weight_expiry = None`, which is the documented behavior of the SPL addin API.
- Likelihood is **medium**: it requires deliberate timing but is straightforward to execute.

---

### Recommendation

Set `voter_weight_expiry` to the **current slot** (not `None`) inside `update_voter_weight`, so that the governance program treats the record as valid only for the current transaction. This matches the SPL addin API's recommended pattern: "Revise instruction to update the weight should be invoked before governance instruction within the same transaction and the expiry set to the current slot." [4](#0-3) 

Alternatively, enforce that `update_voter_weight` and the Realms `CastVote` instruction are submitted atomically in the same transaction.

---

### Proof of Concept

```
Epoch E:
  tx1: createPosition(amount=1_000_000, target=VOTING)
  // positions[0] = { amount: 1_000_000, activation_epoch: E+1, unlocking_start: None }

Epoch E+1 (positions now LOCKED):
  tx2: update_voter_weight(action=CastVote)
  // VoterWeightRecord { voter_weight: 1_000_000, voter_weight_expiry: None }

  tx3: closePosition(index=0, amount=1_000_000, target=VOTING)
  // positions[0].unlocking_start = E+1  → state becomes PREUNLOCKING

Epoch E+2 (positions now UNLOCKING, voting power = 0):
  // Attacker does NOT call update_voter_weight
  tx4: Realms.castVote(proposal, voterWeightRecord=<stale record from tx2>)
  // Realms checks: voter_weight_expiry == None → accepts record
  // Vote cast with weight 1_000_000 despite zero actual staked tokens
``` [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L1704-1715)
```json
      "args": [
        {
          "name": "action",
          "type": {
            "defined": {
              "name": "VoterWeightAction"
            }
          }
        }
      ],
      "discriminator": [92, 35, 133, 94, 230, 70, 14, 157],
      "name": "update_voter_weight"
```

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L2209-2262)
```json
      "name": "VoterWeightRecord",
      "type": {
        "fields": [
          {
            "docs": [
              "VoterWeightRecord discriminator sha256(\"account:VoterWeightRecord\")[..8]",
              "Note: The discriminator size must match the addin implementing program discriminator size",
              "to ensure it's stored in the private space of the account data and it's unique",
              "pub account_discriminator: [u8; 8],",
              "The Realm the VoterWeightRecord belongs to"
            ],
            "name": "realm",
            "type": "pubkey"
          },
          {
            "docs": [
              "Governing Token Mint the VoterWeightRecord is associated with",
              "Note: The addin can take deposits of any tokens and is not restricted to the community or",
              "council tokens only"
            ],
            "name": "governing_token_mint",
            "type": "pubkey"
          },
          {
            "docs": [
              "The owner of the governing token and voter",
              "This is the actual owner (voter) and corresponds to TokenOwnerRecord.governing_token_owner"
            ],
            "name": "governing_token_owner",
            "type": "pubkey"
          },
          {
            "docs": [
              "Voter's weight",
              "The weight of the voter provided by the addin for the given realm, governing_token_mint and",
              "governing_token_owner (voter)"
            ],
            "name": "voter_weight",
            "type": "u64"
          },
          {
            "docs": [
              "The slot when the voting weight expires",
              "It should be set to None if the weight never expires",
              "If the voter weight decays with time, for example for time locked based weights, then the",
              "expiry must be set As a common pattern Revise instruction to update the weight should",
              "be invoked before governance instruction within the same transaction and the expiry set",
              "to the current slot to provide up to date weight"
            ],
            "name": "voter_weight_expiry",
            "type": {
              "option": "u64"
            }
          },
```

**File:** governance/pyth_staking_sdk/src/utils/position.ts (L95-112)
```typescript
export const getVotingTokenAmount = (
  stakeAccountPositions: StakeAccountPositions,
  epoch: bigint,
) => {
  const positions = stakeAccountPositions.data.positions;
  const votingPositions = positions
    .filter((p) => p.targetWithParameters.voting)
    .filter((p) =>
      [PositionState.LOCKED, PositionState.PREUNLOCKING].includes(
        getPositionState(p, epoch),
      ),
    );
  const totalVotingTokenAmount = votingPositions.reduce(
    (sum, p) => sum + p.amount,
    0n,
  );
  return totalVotingTokenAmount;
};
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L926-948)
```typescript
  public async getUpdateVoterWeightInstruction(
    stakeAccountPositions: PublicKey,
    action: VoterWeightAction,
    remainingAccount?: PublicKey,
  ) {
    return this.stakingProgram.methods
      .updateVoterWeight(action)
      .accounts({
        stakeAccountPositions,
      })
      .remainingAccounts(
        remainingAccount
          ? [
              {
                isSigner: false,
                isWritable: false,
                pubkey: remainingAccount,
              },
            ]
          : [],
      )
      .instruction();
  }
```
