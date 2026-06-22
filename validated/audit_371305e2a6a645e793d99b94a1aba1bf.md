I'll analyze the CKB codebase for a vulnerability analogous to the described pattern: an emergency/recovery path that fails to account for an intermediate state, causing permanent loss of user assets.

Let me dig deeper into the specific discrepancy found in the DAO tests and trace where `DaoCalculator` is used in the consensus-critical path.