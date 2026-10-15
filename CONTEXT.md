# HarnessFleet

A local delegation system connecting agents across native harnesses.

## Language

**Harness**: A native agent execution environment such as Codex CLI or Claude Code.
_Avoid_: Provider when referring to the execution environment.

**Route**: A configured combination of harness, environment, account, billing and available models.
_Avoid_: An inferred fallback target.

**Agent Run**: One managed delegated activity with a stable identity across continuations.
_Avoid_: Native session as a synonym.

**Generation**: One bounded invocation or continuation belonging to an Agent Run.
_Avoid_: A new Agent Run for every follow-up.

**Grant**: Preauthorized delegation authority and its finite limits, inherited or narrowed by descendants.
_Avoid_: Visibility as authorization.

**Native Subagent**: A child agent created by a native harness, distinct from managed execution.
_Avoid_: Counting it as a managed delegation level.

**Execution parent**: The managed Agent Run whose grant authorizes a child run.
_Avoid_: Treating it as the exact native caller.

**Native caller**: The attributable native session or subagent that requested a managed delegation.
_Avoid_: Inferring it from cwd or process proximity.

**Observation**: Read-only visibility of a connected native session.
_Avoid_: Attachment or control.

**Attachment**: Explicit admission of a supported native session into managed control.
_Avoid_: Assuming an observed session is available for execution.

**Receipt**: Evidence bound to one invocation generation and its verified outcome.
_Avoid_: Treating a missing receipt as permission to repeat a consumed request.


**Delegation capability**: Authority to request child operations under one active parent generation.
_Avoid_: Host administration or proof of worker isolation.

**Worker isolation**: Enforced separation preventing a native worker from accessing another run's private state and control processes.
_Avoid_: A scoped delegation capability as a synonym.
