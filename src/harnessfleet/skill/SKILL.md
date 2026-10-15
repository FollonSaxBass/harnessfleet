---
name: harnessfleet
description: Delegate bounded assignments between installed agent harnesses through the local HarnessFleet CLI.
---

# HarnessFleet delegation

Use the installed `harnessfleet` command. Native CLIs and accounts remain external prerequisites.

1. From an unmanaged native session, check `harnessfleet service status` first.
   Its controller must already be running from a host terminal before you launch
   agents. A native tool may terminate detached processes when it returns. Managed
   parents have their own host broker. Do not try to repair lifecycle failures by
   repeating a consumed launch. Run `harnessfleet run routes --diagnose`. Inspect the structured capabilities,
   available Routes, models, billing and platform support. Doctor never invokes models.
2. Interpret the user's selection. A required model stays required. A required set
   means every member participates or has an explicit failure. An allowed set permits
   choosing members. Clarify ambiguous participation before launching. With no user
   constraint, choose suitable available targets within the preauthorized policy.
3. Select an exact `--route` and `--model`. Never substitute another account,
   provider, harness or billing mode. Subscription is the default. Metered needs both
   an explicit user selection and policy authorization. Experimental support needs
   policy opt-in; availability is not certification.
4. Give a bounded assignment with acceptance criteria and explicit file references.
   Prepare the working directory yourself; HarnessFleet never creates worktrees.
   Do not export your conversation, account configuration, credentials or unrelated files.
5. Launch using:

   ```sh
   harnessfleet run start --route ROUTE --model MODEL -C DIRECTORY --permission read-only "ASSIGNMENT"
   ```

   Output is JSON containing `id`. Use `run batch --targets '[{"route":"R","model":"M"}]'
   --selection '{"required":[{"route":"R","model":"M"}]}' "ASSIGNMENT"` for required sets.
   Allowed sets use `--selection '{"allowed":[...]}'`. Inspect every target's result.
   The core validates resolved targets; it does not infer natural-language intent.
6. Await `harnessfleet run follow ID --timeout 120`. This blocks on execution events.
   Keep a long follower in your harness's background tool and await its completion;
   do not repeatedly poll unchanged logs. A timeout leaves the child running.
7. Read `harnessfleet run output ID`. Integrate only verified final output and check
   the child's acceptance criteria. `--partial` exposes unverified evidence for inspection.
   Failure to obtain a final receipt is never permission to repeat a consumed invocation.
8. On capacity refusal decide whether to request `--queue`; no implicit queueing.
   `run cancel ID` cancels queued work; `run stop ID` stops managed execution.
   `run recover` reconciles lost supervisors and preserves uncertain ownership.
9. `run resume ID "BOUNDED FOLLOW-UP"` preserves identity with a new generation
   only on a supported verified session. Automatic quota continuation is opt-in,
   finite and capability-gated; no built-in route currently certifies quota recovery.

Managed authority is inherited through launcher-generated environment values. Do
not invent parent ids or tokens. Descendants cannot broaden the grant; managed depth
is limited to two. Native subagent depth is separate. Box workers cannot delegate.
An observed native session is read-only and is not an execution grant.

Codex `read-only` uses its native sandbox. Claude `read-only` exposes Read/Glob/Grep;
`workspace-write` adds file editing. Claude `delegate` additionally enables Bash,
which can modify files and run commands without OS containment: request it only
when the user's grant authorizes it. Codex `workspace-write` allows the configured
state directory so delegated CLI calls can register their results. Native tool
restrictions are not general process or network containment.
