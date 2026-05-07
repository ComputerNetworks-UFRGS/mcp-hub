# Agent Prompts

System prompts for the multi-agent Kubernetes observability system.

## Agents

| File                   | Agent       | Description                                                                                                                                                       |
|------------------------|-------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `orchestrator.yaml`    | Orchestrator | Triages the user's problem, delegates investigation tasks to specialized agents one at a time, and synthesizes their findings into a root-cause diagnosis.        |
| `agent_metrics.yaml`   | Metrics      | Queries Prometheus to detect resource bottlenecks. Summarizes CPU, memory, and latency behavior during an incident window and compares it against a baseline.     |
| `agent_logs.yaml`      | Logs         | Filters container logs for errors, exceptions, and warnings. Groups repeated errors and extracts stack trace origins without returning raw log dumps.             |
| `agent_traces.yaml`    | Traces       | Queries Jaeger for distributed traces with errors or latency outliers. Maps the call chain that led to a failure and pinpoints the span where it originated.      |
| `agent_kubernetes.yaml`| Kubernetes   | Inspects cluster state via kubectl. Reports failing pods, degraded deployments, misconfigured resources, and recent warning events within a given namespace.      |
| `agent_github.yaml`    | GitHub       | Searches the application's GitHub repository to correlate runtime errors with source code, known issues, and recent changes that may have introduced a regression.|

## File structure

Each file contains a `versions` list, newest first.

```yaml
versions:
  - id: agent_logs
    version: "1.1"
    last_updated: "2025-05-10"
    description: Brief note on what changed and why.
    system_prompt: |
      ...
```

## Versioning convention

- Bump the **minor** version (1.0 → 1.1) for wording tweaks or rule adjustments.
- Bump the **major** version (1.x → 2.0) when the output format changes, since
  that affects how the orchestrator parses the response.
- Keep old versions in the file while actively comparing them. Once a version
  is clearly superseded and no longer useful as a reference, remove it.
