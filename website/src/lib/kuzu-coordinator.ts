// website/src/lib/kuzu-coordinator.ts
import { createClient, SupabaseClient } from "@supabase/supabase-js";

export type QueryExecutionCallback = (
  queryType: string,
  target: string,
  params: any
) => Promise<any>;

export type ToolsListCallback = () => Promise<any[]>;

export type ToolCallCallback = (
  toolName: string,
  args: any
) => Promise<any>;

export class KuzuCoordinator {
  private supabase: SupabaseClient;
  private channelName: string;
  private channel: any = null;
  private globalChannel: any = null;
  
  private executeQueryCallback: QueryExecutionCallback;
  private getToolsCallback: ToolsListCallback;
  private executeToolCallback: ToolCallCallback;
  
  private isSubscribed = false;
  private isStarted = false; // Tracks if start() has been explicitly called by the parent component
  private keepaliveInterval: ReturnType<typeof setInterval> | null = null;

  constructor(
    supabaseUrl: string,
    supabaseAnonKey: string,
    channelName: string,
    executeQueryCallback: QueryExecutionCallback,
    getToolsCallback: ToolsListCallback,
    executeToolCallback: ToolCallCallback
  ) {
    if (!supabaseUrl || !supabaseAnonKey) {
      console.warn("[KuzuCoordinator] Missing Supabase configuration credentials.");
    }
    this.supabase = createClient(supabaseUrl, supabaseAnonKey);
    this.channelName = channelName;
    this.executeQueryCallback = executeQueryCallback;
    this.getToolsCallback = getToolsCallback;
    this.executeToolCallback = executeToolCallback;
  }

  private handleVisibilityChange = async () => {
    if (document.visibilityState === "visible" && this.isStarted) {
      console.log("[KuzuCoordinator] Page became visible/active. Self-healing WebSocket connections...");
      // Forcibly tear down stale channel handles and await completion to prevent race conditions
      await this.stop(true); // Keep isStarted flag true
      this.start();
    }
  };

  /**
   * Subscribes to the real-time signaling channels and listens for queries/MCP events.
   */
  public start() {
    this.isStarted = true;
    if (this.isSubscribed) return;

    // Register visibility change listener to auto-heal from background tab sleep/suspension
    if (typeof window !== "undefined" && typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", this.handleVisibilityChange);
      document.addEventListener("visibilitychange", this.handleVisibilityChange);
    }

    console.log(`[KuzuCoordinator] Subscribing to query channel: ${this.channelName}`);
    this.channel = this.supabase.channel(this.channelName);

    // 1. Listen for standard query requests and MCP tool calls on the repo channel
    this.channel
      .on(
        "broadcast",
        { event: "query-request" },
        async ({ payload }: { payload: any }) => {
          const { id, queryType, target, params } = payload || {};
          if (!id) return;
          console.log(`[KuzuCoordinator] 📥 Query request received: id=${id}, type=${queryType}`);
          try {
            const result = await this.executeQueryCallback(queryType, target, params);
            await this.channel.send({
              type: "broadcast",
              event: "query-response",
              payload: { id, status: "success", result }
            });
          } catch (err: any) {
            await this.channel.send({
              type: "broadcast",
              event: "query-response",
              payload: { id, status: "error", error: err.message }
            });
          }
        }
      )
      .on(
        "broadcast",
        { event: "tool-call-request" },
        async ({ payload }: { payload: any }) => {
          const { id, toolName, args } = payload || {};
          if (!id || !toolName) return;
          console.log(`[KuzuCoordinator] 📥 MCP Tool Call request received: id=${id}, name=${toolName}`);
          try {
            const result = await this.executeToolCallback(toolName, args);
            await this.channel.send({
              type: "broadcast",
              event: "tool-call-response",
              payload: { id, status: "success", result }
            });
          } catch (err: any) {
            await this.channel.send({
              type: "broadcast",
              event: "tool-call-response",
              payload: { id, status: "error", error: err.message }
            });
          }
        }
      )
      .subscribe((status: string) => {
        if (status === "SUBSCRIBED") {
          console.log(`[KuzuCoordinator] ✅ Subscribed to query channel: ${this.channelName}`);
        }
      });

    // 2. Listen for tools list queries on the global channel
    const globalChannelName = "cgc-tunnel-global-mcp";
    console.log(`[KuzuCoordinator] Subscribing to global channel: ${globalChannelName}`);
    this.globalChannel = this.supabase.channel(globalChannelName);

    this.globalChannel
      .on(
        "broadcast",
        { event: "tools-list-request" },
        async ({ payload }: { payload: any }) => {
          const { id } = payload || {};
          if (!id) return;
          console.log(`[KuzuCoordinator] 📥 Tools List request received: id=${id}`);
          try {
            const tools = await this.getToolsCallback();
            await this.globalChannel.send({
              type: "broadcast",
              event: "tools-list-response",
              payload: { id, status: "success", tools }
            });
          } catch (err: any) {
            await this.globalChannel.send({
              type: "broadcast",
              event: "tools-list-response",
              payload: { id, status: "error", error: err.message }
            });
          }
        }
      )
      .on(
        "broadcast",
        { event: "tool-call-request" },
        async ({ payload }: { payload: any }) => {
          const { id, toolName, args } = payload || {};
          if (!id || !toolName) return;
          console.log(`[KuzuCoordinator] 📥 Global MCP Tool Call request received: id=${id}, name=${toolName}`);
          try {
            const result = await this.executeToolCallback(toolName, args);
            await this.globalChannel.send({
              type: "broadcast",
              event: "tool-call-response",
              payload: { id, status: "success", result }
            });
          } catch (err: any) {
            await this.globalChannel.send({
              type: "broadcast",
              event: "tool-call-response",
              payload: { id, status: "error", error: err.message }
            });
          }
        }
      )
      .subscribe((status: string) => {
        if (status === "SUBSCRIBED") {
          this.isSubscribed = true;
          console.log(`[KuzuCoordinator] ✅ Subscribed to global channel: ${globalChannelName}`);
        }
      });

    // Keep WebSocket warm when ChatGPT tab steals focus (Firefox/Chrome throttle background tabs)
    if (this.keepaliveInterval) clearInterval(this.keepaliveInterval);
    this.keepaliveInterval = setInterval(() => {
      if (!this.isStarted) return;
      try {
        this.globalChannel?.send({
          type: "broadcast",
          event: "tunnel-keepalive",
          payload: { t: Date.now() }
        });
      } catch {
        /* ignore */
      }
    }, 15000);
  }

  public async stop(keepStarted = false) {
    if (!keepStarted) {
      this.isStarted = false;
      if (typeof window !== "undefined" && typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", this.handleVisibilityChange);
      }
    }
    if (this.channel) {
      console.log(`[KuzuCoordinator] Unsubscribing from query tunnel: ${this.channelName}`);
      try {
        await this.supabase.removeChannel(this.channel);
      } catch (err) {}
      this.channel = null;
    }
    if (this.globalChannel) {
      console.log(`[KuzuCoordinator] Unsubscribing from global tools tunnel`);
      try {
        await this.supabase.removeChannel(this.globalChannel);
      } catch (err) {}
      this.globalChannel = null;
    }
    if (this.keepaliveInterval) {
      clearInterval(this.keepaliveInterval);
      this.keepaliveInterval = null;
    }
    this.isSubscribed = false;
  }
}
