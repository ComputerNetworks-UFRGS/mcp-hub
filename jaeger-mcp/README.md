# Jaeger MCP Server
Jaeger MCP Server, uses georgeezejiofor/jaeger-mcp:

```
jaeger-mcp — Jaeger Distributed Tracing MCP Server

Dedicated Jaeger Query API specialist for in-depth trace analysis.
Complements otel-mcp (which does Jaeger + ES log correlation).
This server goes deeper into pure Jaeger analysis:
  - Service dependency graph (call counts, downstream chains)
  - Side-by-side trace comparison (find regressions)
  - Per-service latency breakdown within a trace
  - Tag/resource attribute filtering for precise fault localisation

Tools:
  list_jaeger_services         — all services with active traces
  get_jaeger_operations        — operations/endpoints per service
  search_jaeger_traces         — search by service, operation, tags, time window
  get_jaeger_trace             — full span-level detail for a trace ID
  get_slow_jaeger_traces       — traces above a latency threshold
  get_error_jaeger_traces      — traces containing error spans
  get_jaeger_dependencies      — service dependency graph (call counts)
  compare_jaeger_traces        — diff two traces: duration, spans, error delta
  get_trace_service_breakdown  — per-service latency breakdown within one trace
  find_traces_by_tag           — find traces where any span has a specific tag value

Backend:
  Jaeger Query API → JAEGER_URL (default: jaeger.monitoring.svc.cluster.local:16686)

Transport: streamable-http  Port: 8000  Path: /mcp
```