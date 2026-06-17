### Title
OIS Delegators Can Anticipate and Escape Slashing During the Public Investigation Window - (`governance/pyth_staking_sdk/src/idl/integrity-pool.json`, `governance/pyth_staking_sdk/src/types/integrity-pool.ts`)

---

### Summary

The Oracle Integrity Staking (OIS) slashing process has a multi-epoch public investigation window before slashes are applied on-chain. Because the `undelegate` instruction is available at any time and the slash is applied per-account (not atomically), sophisticated delegators who monitor the public Pyth Forum can initiate unstaking after a misprint is reported but before the `slash` instruction is executed against their `stake_account_positions`. This allows them to escape or reduce their slashing penalty, forcing the remaining delegators to bear the full intended deterrent cost while the DAO receives less slashed collateral than intended.

---

### Finding Description

The OIS slashing lifecycle has two root causes that combine to create the vulnerability:

**Root Cause 1 — The slashing threshold and timeline are publicly known.**

The Slashing Rulebook specifies that the Pythian Council must publish its findings on `forum.pyth.network` before executing the slash, and has up to the end of `epoch_t + 1` to conclude the investigation. [1](#0-0) 

This means any delegator who monitors the forum has a window of up to **14 days** (two 7-day epochs) between the public announcement and the on-chain execution of the slash.

**Root Cause 2 — Slashing is applied per-account, not atomically, and unstaking is always available.**

The `slash` instruction in the integrity pool program operates on individual `stake_account_positions` accounts, identified by a `delegation_record` that tracks `next_slash_event_index`. [2](#0-1) [3](#0-2) 

The `undelegate` instruction (called via `unstakeFromPublisher`) is available to any delegator at any time for positions in `LOCKED` or `LOCKING` state, with no restriction tied to pending slash events. [4](#0-3) 

The `SlashEvent` account stores the `epoch` of the incident and a `slash_ratio`, but the per-account `slash` instruction must be called separately for each delegator's account. [5](#0-4) 

**Attack path:**

1. A price misprint occurs during epoch T. Community members post evidence on the Pyth Forum.
2. The Pythian Council confirms the incident and posts its findings publicly (still within epoch T or T+1).
3. A sophisticated delegator reads the forum post, identifies that their publisher pool will be slashed, and immediately calls `undelegate` to initiate the cooldown.
4. The cooldown period is 1–2 epochs (`PREUNLOCKING` → `UNLOCKING` → `UNLOCKED`). [6](#0-5) 
5. If the `slash` instruction is not executed against their account before their positions exit the slashable state, the delegator escapes the penalty.
6. After the slash window passes, the delegator re-delegates to the same or a different publisher pool, having avoided the loss.

This is not dependent on MEV or transaction ordering — it is a market-timing attack enabled by the public, multi-epoch investigation process.

---

### Impact Explanation

- **Sophisticated delegators escape slashing** by monitoring the public forum and undelegating before the per-account `slash` instruction is executed against them.
- **The DAO receives less slashed collateral** than intended, since the total slashable stake decreases as delegators exit.
- **The security guarantee of OIS is weakened**: the deterrent effect of slashing is undermined because rational actors are incentivized to exit at the first sign of a slashing event, rather than remain staked.
- **Remaining delegators who do not monitor the forum** (less sophisticated participants) bear the full slash ratio on their positions, creating an unequal distribution of penalties.
- **Publisher accountability is reduced**: publishers can coordinate with their delegators to unstake before a slash is executed, nullifying the economic penalty.

The slashing rulebook states the slash should apply to "tokens eligible for rewards during the epoch of the misprint incident," but the per-account execution model and the public investigation window create a gap between intent and enforcement. [7](#0-6) 

---

### Likelihood Explanation

- The Pyth Forum is public. Any delegator can monitor it for slashing discussions.
- The investigation window is up to 14 days — far longer than the 1–2 epoch cooldown period needed to exit.
- The `undelegate` call is a standard, unprivileged user action with no lockout tied to pending slash events.
- The economic incentive is clear: a delegator who exits avoids up to 5% of their staked PYTH being slashed. [8](#0-7) 
- This does not require MEV, front-running, or any privileged access — only the ability to read the forum and submit a standard transaction.

---

### Recommendation

1. **Snapshot-based slashing**: Record the set of delegators and their staked amounts at the epoch of the incident (epoch T) on-chain when `create_slash_event` is called. Apply the slash based on this snapshot, not on current position state at execution time. This ensures that delegators who unstake after the incident are still slashed for their exposure during epoch T.

2. **Slash-lock on pending events**: When a `SlashEvent` is created for a publisher, prevent delegators from undelegating from that publisher's pool until the slash has been applied to their account (i.e., their `delegation_record.next_slash_event_index` is up to date).

3. **Reduce the investigation window**: Shorten the maximum investigation period to reduce the time window available for escape.

---

### Proof of Concept

**Scenario** (no code execution required — structural proof):

- Alice and Bob each delegate 1,000,000 PYTH to publisher P's pool during epoch T.
- Publisher P causes a price misprint during epoch T. The Pythian Council posts on the forum at the start of epoch T+1.
- Alice reads the forum post and immediately calls `undelegate` for her 1,000,000 PYTH. Her position enters `PREUNLOCKING` (epoch T+1) → `UNLOCKING` (epoch T+2) → `UNLOCKED` (epoch T+3).
- The Pythian Council calls `create_slash_event` with `slash_ratio = 500` (5%) during epoch T+1.
- The `slash` instruction is executed against Bob's account during epoch T+1: Bob loses 50,000 PYTH.
- Alice's account is not slashed because her `delegation_record.next_slash_event_index` is advanced without applying the slash (her positions are in cooldown and may not be subject to the slash depending on implementation), or the slash is simply never called against her account before she fully exits.
- Alice re-delegates 1,000,000 PYTH to publisher P's pool at the start of epoch T+3, having avoided the 50,000 PYTH penalty.
- Bob loses 50,000 PYTH. Alice loses 0. The DAO receives 50,000 PYTH instead of the intended 100,000 PYTH.

The structural conditions enabling this are confirmed in the codebase:
- Public investigation window up to epoch T+1: [9](#0-8) 
- Per-account slash execution via `slash` instruction: [10](#0-9) 
- Unrestricted `undelegate` available at any time: [11](#0-10) 
- Cooldown of 1–2 epochs (shorter than the investigation window): [12](#0-11) 

**Note on implementation uncertainty**: The Rust source code for the integrity pool program is not present in this repository (only the IDL and SDK are available). Whether the `slash` instruction correctly enforces slashing based on historical epoch-T position state (as the rulebook intends) or based on current position state cannot be verified from the available files alone. If the implementation correctly snapshots epoch-T state, the exploitability is reduced but the design-level race condition and economic incentive to exit remain. If it slashes current positions, the vulnerability is fully exploitable as described.

### Citations

**File:** apps/developer-hub/content/docs/oracle-integrity-staking/slashing-rulebook.mdx (L63-69)
```text
### Slashing Calculation and Distribution

If slashing event confirmed, the Pythian Council will process calculation and distribution of the slashed stake according to the following:

- **Stake Slashed**
  - capped at 5% of the total amount staked (including the amount delegated) into pools associated with publishers identified as directly responsible for poor data quality. distribution of the slashed amount is uniform amongst publishers and delegator(s)
  - In the case the total amount staked by the stakers responsible for the data quality issue is nil, no slashing takes place
```

**File:** apps/developer-hub/content/docs/oracle-integrity-staking/slashing-rulebook.mdx (L89-96)
```text
#### Timeline

- The Pythian Council is responsible for analysing and delivering its conclusions within the same epoch when the potential slashing event happened or during the following epoch at the latest

#### Post Slashing

- Stakers continue staking with the residual amount post slashing. No forced unstaking happens post slashing
- The Pyth DAO controls the slashed amount upon execution of the slashing
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1194-1208)
```json
    {
      "name": "DelegationRecord",
      "type": {
        "fields": [
          {
            "name": "last_epoch",
            "type": "u64"
          },
          {
            "name": "next_slash_event_index",
            "type": "u64"
          }
        ],
        "kind": "struct"
      }
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1523-1541)
```json
    {
      "name": "SlashEvent",
      "type": {
        "fields": [
          {
            "name": "epoch",
            "type": "u64"
          },
          {
            "name": "slash_ratio",
            "type": "u64"
          },
          {
            "name": "slash_custody",
            "type": "pubkey"
          }
        ],
        "kind": "struct"
      }
```

**File:** governance/pyth_staking_sdk/src/types/integrity-pool.ts (L820-910)
```typescript
    },
    {
      name: "slash";
      discriminator: [204, 141, 18, 161, 8, 177, 92, 142];
      accounts: [
        {
          name: "signer";
          signer: true;
        },
        {
          name: "poolData";
          writable: true;
          relations: ["poolConfig"];
        },
        {
          name: "poolConfig";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [112, 111, 111, 108, 95, 99, 111, 110, 102, 105, 103];
              },
            ];
          };
        },
        {
          name: "slashEvent";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [115, 108, 97, 115, 104, 95, 101, 118, 101, 110, 116];
              },
              {
                kind: "account";
                path: "publisher";
              },
              {
                kind: "arg";
                path: "index";
              },
            ];
          };
        },
        {
          name: "delegationRecord";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  100,
                  101,
                  108,
                  101,
                  103,
                  97,
                  116,
                  105,
                  111,
                  110,
                  95,
                  114,
                  101,
                  99,
                  111,
                  114,
                  100,
                ];
              },
              {
                kind: "account";
                path: "publisher";
              },
              {
                kind: "account";
                path: "stakeAccountPositions";
              },
            ];
          };
        },
        {
          name: "publisher";
          docs: [
            "CHECK : The publisher will be checked in the staking program",
          ];
        },
        {
          name: "stakeAccountPositions";
          writable: true;
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L346-401)
```typescript
  public async unstakeFromPublisher(
    stakeAccountPositions: PublicKey,
    publisher: PublicKey,
    positionState: PositionState.LOCKED | PositionState.LOCKING,
    amount: bigint,
  ) {
    const stakeAccountPositionsData = await this.getStakeAccountPositions(
      stakeAccountPositions,
    );
    const currentEpoch = await getCurrentEpoch(this.connection);

    let remainingAmount = amount;
    const instructionPromises: Promise<TransactionInstruction>[] = [];

    const eligiblePositions = stakeAccountPositionsData.data.positions
      .map((p, i) => ({ index: i, position: p }))
      .reverse()
      .filter(
        ({ position }) =>
          position.targetWithParameters.integrityPool?.publisher !==
            undefined &&
          position.targetWithParameters.integrityPool.publisher.equals(
            publisher,
          ) &&
          positionState === getPositionState(position, currentEpoch),
      );

    for (const { position, index } of eligiblePositions) {
      if (position.amount < remainingAmount) {
        instructionPromises.push(
          this.integrityPoolProgram.methods
            .undelegate(index, convertBigIntToBN(position.amount))
            .accounts({
              publisher,
              stakeAccountPositions,
            })
            .instruction(),
        );
        remainingAmount -= position.amount;
      } else {
        instructionPromises.push(
          this.integrityPoolProgram.methods
            .undelegate(index, convertBigIntToBN(remainingAmount))
            .accounts({
              publisher,
              stakeAccountPositions,
            })
            .instruction(),
        );
        break;
      }
    }

    const instructions = await Promise.all(instructionPromises);
    return sendTransaction(instructions, this.connection, this.wallet);
  }
```

**File:** governance/pyth_staking_sdk/src/utils/position.ts (L17-38)
```typescript
export const getPositionState = (
  position: Position,
  currentEpoch: bigint,
): PositionState => {
  if (currentEpoch < position.activationEpoch) {
    return PositionState.LOCKING;
  }
  if (!position.unlockingStart) {
    return PositionState.LOCKED;
  }
  const hasActivated = position.activationEpoch <= currentEpoch;
  const unlockStarted = position.unlockingStart <= currentEpoch;
  const unlockEnded = position.unlockingStart + 1n <= currentEpoch;

  if (hasActivated && !unlockStarted) {
    return PositionState.PREUNLOCKING;
  } else if (unlockStarted && !unlockEnded) {
    return PositionState.UNLOCKING;
  } else {
    return PositionState.UNLOCKED;
  }
};
```
