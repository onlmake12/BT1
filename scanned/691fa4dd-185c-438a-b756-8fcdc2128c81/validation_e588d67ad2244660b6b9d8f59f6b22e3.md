Looking at the vulnerability class — **missing access control on state-modifying functions callable by any unprivileged caller** — I need to find the CKB analog where an RPC caller (explicitly in scope) can invoke destructive operations without any authentication.

Let me examine the RPC pool and net modules in detail.