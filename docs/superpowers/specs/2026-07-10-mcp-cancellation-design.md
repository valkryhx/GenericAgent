# MCP Cancellation and Cleanup Design

## Problem

An MCP timeout can leave GenericAgent blocked inside the synchronous bridge to its asyncio MCP loop. The current patch polls a `concurrent.futures.Future` and returns after calling `future.cancel()`, but that future can report cancellation before the underlying asyncio task and MCP transport have exited. It also does not make the initial connection wait cancellable and incorrectly marks user cancellation as a server failure.

## Design

Each `GenericAgentHandler` owns one `threading.Event`-compatible cancellation signal for its turn. `GenericAgent.abort()` and workflow child cancellation set that event. MCP dispatch installs the event in a thread-local call context, preserving the public two-argument `call_mcp_tool(name, arguments)` call shape used by plugins and tests.

The MCP manager separates two ownership domains:

- Connection startup is manager-owned and shared per server by discovery and tool callers. A cancelled or short-timeout turn stops waiting without cancelling the shared startup or classifying the server as failed. Manager shutdown cancels and drains startup work.
- A tool invocation is turn-owned. Cancellation or timeout cancels the asyncio task, waits briefly for cooperative exit, then force-closes the FastMCP client/transport if needed and waits for cleanup to finish.

Schema discovery runs inside the same turn cancellation scope. A cancelled discovery returns without cancelling shared startup and does not persist an incomplete tool cache.

FastMCP 2.14.4 already closes stdio process trees on Windows and Unix. GenericAgent will invoke `Client.close()` for forced cleanup and will not duplicate platform process-killing code.

## Result Semantics

- User cancellation returns an MCP error containing `aborted by user` and does not set server status to `failed`.
- Tool timeout returns a timeout error and does not set server status to `failed`.
- If cancellation or timeout requires closing the connection, the server returns to `pending` while retaining discovered tool metadata so the next call can reconnect.
- Protocol, startup, or transport failures continue to set server status to `failed`.

## Workflow Integration

`NativeGPTChildAgentRunner` stores the active handler in job state. `cancel(job)` cancels both the active LLM request and the handler turn event. Cancellation remains effective if it races with handler construction or schema discovery. Once cancelled, the child stops consuming the agent loop before another model request and publishes `AgentResult(status="cancelled")`.

## Verification

Regression coverage must exercise public paths:

1. `GenericAgent.abort()` interrupts a tool while its MCP server is still connecting.
2. `/stop` during an in-flight tool call returns promptly, drains asyncio work, and terminates the stdio server process.
3. Timeout leaves no pending asyncio task warning and permits a later reconnect.
4. Cancellation and timeout do not mark the server failed.
5. Workflow child cancellation reaches an MCP tool call.
6. Existing two-argument MCP dispatch mocks remain compatible.
7. Concurrent discovery starts each MCP server once and closes every owned stderr handle.
8. A short-timeout waiter does not cancel a longer waiter sharing the same startup.
9. Main-agent and workflow discovery cancellation does not call the model or write an empty cache.
