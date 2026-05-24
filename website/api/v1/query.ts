// website/api/v1/query.ts
import { createClient } from "@supabase/supabase-js";

export default async function handler(req: any, res: any) {
  // Enable CORS
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");

  if (req.method === "OPTIONS") {
    return res.status(200).end();
  }

  const method = req.method;
  const params = method === "POST" ? (req.body || {}) : (req.query || {});
  const { repo, query_type, target, cypher_query } = params;

  if (!query_type || typeof query_type !== "string") {
    return res.status(400).json({ 
      error: "Missing required parameter 'query_type'. Expected: 'definitions', 'callers', 'callees', 'file_structure', or 'cypher'." 
    });
  }

  const isGlobalTool = query_type === "list_indexed_repositories" || query_type === "search_registry_bundles";

  if (!isGlobalTool) {
    if (!repo || typeof repo !== "string") {
      return res.status(400).json({ error: "Missing required parameter 'repo' (owner/repo)." });
    }
  }

  const supabaseUrl = process.env.VITE_SUPABASE_URL || process.env.SUPABASE_URL;
  const supabaseAnonKey = process.env.VITE_SUPABASE_ANON_KEY || process.env.SUPABASE_ANON_KEY;

  if (!supabaseUrl || !supabaseAnonKey) {
    return res.status(500).json({
      error: "Server configuration error: Supabase credentials are not configured on Vercel."
    });
  }

  const supabase = createClient(supabaseUrl, supabaseAnonKey);
  
  const wasmQueries = ["definitions", "callers", "callees", "file_structure", "search", "cypher"];
  const isWasmQuery = wasmQueries.includes(query_type);

  let channelName = "cgc-tunnel-global-mcp";
  let cleanRepo = "";

  if (repo && typeof repo === "string") {
    cleanRepo = repo.trim().replace(/^(https?:\/\/)?(www\.)?github\.com\//, "").replace(/\/$/, "");
  }

  // Only Kuzu WASM queries are repository-scoped (since they require active visualization rendering).
  // All MCP Python tools are background-capable and are routed globally!
  if (isWasmQuery && !isGlobalTool && cleanRepo) {
    const cleanRepoName = cleanRepo.replace(/\//g, "_").toLowerCase();
    channelName = `cgc-tunnel-${cleanRepoName}`;
  }

  const channel = supabase.channel(channelName);

  const requestId = Math.random().toString(36).substring(2, 15);
  let hasResponded = false;

  // Cleanup helper
  const cleanup = () => {
    try {
      supabase.removeChannel(channel);
    } catch (err) {}
  };

  try {
    if (isWasmQuery) {
      // 1. Standard Kuzu WASM Query Execution
      let wasmResponse: any = null;
      let resolveWaitPromise: (() => void) | null = null;
      const waitPromise = new Promise<void>((resolve) => {
        resolveWaitPromise = resolve;
      });

      channel.on(
        "broadcast",
        { event: "query-response" },
        ({ payload }: { payload: any }) => {
          if (payload && payload.id === requestId) {
            hasResponded = true;
            wasmResponse = payload;
            cleanup();
            if (resolveWaitPromise) resolveWaitPromise();
          }
        }
      );

      await new Promise<void>((resolve, reject) => {
        channel.subscribe((status: string) => {
          if (status === "SUBSCRIBED") {
            resolve();
          } else if (status === "CLOSED" || status === "TIMED_OUT") {
            reject(new Error(`Failed to subscribe to tunnel channel: ${status}`));
          }
        });
      });

      // Dispatch query-request to the active browser tab
      const sendStatus = await channel.send({
        type: "broadcast",
        event: "query-request",
        payload: {
          id: requestId,
          queryType: query_type,
          target: target || cypher_query || "",
          params: {
            cypher_query,
            repo: cleanRepo
          }
        }
      });

      if (sendStatus !== "ok") {
        cleanup();
        return res.status(502).json({
          error: "Failed to broadcast query to the signaling tunnel.",
          details: sendStatus
        });
      }

      // Wait up to 4.5 seconds, but resolve IMMEDIATELY as soon as the client responds!
      const safetyTimeout = setTimeout(() => {
        if (resolveWaitPromise) resolveWaitPromise();
      }, 4500);

      await waitPromise;
      clearTimeout(safetyTimeout);

      if (!hasResponded) {
        return res.status(412).json({
          status: "offline",
          error: "Browser-as-a-Server dashboard is currently offline or closed.",
          message: `To allow your AI assistant to query the graph of ${cleanRepo}, please keep https://cgc.codes/explore open in an active browser tab. Kuzu WASM will automatically boot locally and process your requests instantly.`
        });
      }

      if (wasmResponse?.status === "success") {
        return res.status(200).json(wasmResponse.result);
      } else {
        return res.status(500).json({
          error: "Query execution failed inside client Kuzu WASM database.",
          details: wasmResponse?.error
        });
      }

    } else {
      // 2. Dynamic Python MCP Tool execution (e.g. find_dead_code, calculate_cyclomatic_complexity, etc.)
      let toolResponse: any = null;
      let resolveWaitPromise: (() => void) | null = null;
      const waitPromise = new Promise<void>((resolve) => {
        resolveWaitPromise = resolve;
      });

      channel.on(
        "broadcast",
        { event: "tool-call-response" },
        ({ payload }: { payload: any }) => {
          if (payload && payload.id === requestId) {
            hasResponded = true;
            toolResponse = payload;
            cleanup();
            if (resolveWaitPromise) resolveWaitPromise();
          }
        }
      );

      await new Promise<void>((resolve, reject) => {
        channel.subscribe((status: string) => {
          if (status === "SUBSCRIBED") {
            resolve();
          } else if (status === "CLOSED" || status === "TIMED_OUT") {
            reject(new Error(`Failed to subscribe to tunnel channel: ${status}`));
          }
        });
      });

      // Prepare arguments (pass all params, ensuring repo is set)
      const toolArgs = {
        repo: cleanRepo,
        ...params
      };

      const sendStatus = await channel.send({
        type: "broadcast",
        event: "tool-call-request",
        payload: {
          id: requestId,
          toolName: query_type,
          args: toolArgs
        }
      });

      if (sendStatus !== "ok") {
        cleanup();
        return res.status(502).json({
          error: `Failed to broadcast Python tool '${query_type}' to the signaling tunnel.`,
          details: sendStatus
        });
      }

      // Wait up to 4.5 seconds, but resolve IMMEDIATELY as soon as the client responds!
      const safetyTimeout = setTimeout(() => {
        if (resolveWaitPromise) resolveWaitPromise();
      }, 4500);

      await waitPromise;
      clearTimeout(safetyTimeout);

      if (!hasResponded) {
        return res.status(412).json({
          status: "offline",
          error: "Browser-as-a-Server dashboard is currently offline or closed.",
          message: `To run Python tool '${query_type}', please open https://cgc.codes/explore in an active browser tab. Pyodide will automatically execute the analysis in the background.`
        });
      }

      if (toolResponse?.status === "error") {
        return res.status(500).json({
          error: `Python MCP execution failed for tool '${query_type}'.`,
          details: toolResponse.error
        });
      }

      // Return the exact result content payload from Python MCPServer
      return res.status(200).json(toolResponse?.result);
    }

  } catch (error: any) {
    cleanup();
    console.error("Signaling tunnel query error:", error);
    return res.status(500).json({
      error: "Signaling gateway failed to execute tunnel query.",
      details: error.message
    });
  }
}
