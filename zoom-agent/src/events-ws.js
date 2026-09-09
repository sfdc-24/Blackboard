// Event plane: outbound WebSocket to Zoom (the "doorbell").
// Receives meeting.rtms_started / stopped / interrupted and hands them to a handler.
// The media itself flows over a separate RTMS socket managed in rtms.js.
import WebSocket from 'ws';
import { config } from './config.js';
import { getAccessToken } from './oauth.js';

const PING_INTERVAL_MS = 25_000;       // keep-alive cadence
const TOKEN_RECYCLE_MS = 50 * 60_000;  // reconnect before the 1h token expires
const MAX_BACKOFF_MS = 30_000;

// What the recycle actually needs: a token that outlives the NEXT recycle
// window, not merely one that has not expired yet. Asking for five minutes of
// remaining life — the old default — was satisfied by the token already in
// hand, so the "recycle with a fresh access token" reconnected with the same
// one and the 1-hour expiry it existed to dodge arrived anyway.
const RECYCLE_MIN_TTL_MS = TOKEN_RECYCLE_MS + 5 * 60_000;

export class ZoomEventSocket {
  constructor(onEvent) {
    this.onEvent = onEvent;
    this.ws = null;
    this.pingTimer = null;
    this.recycleTimer = null;
    this.attempts = 0;
    this.intentionalClose = false;
    this.recycling = false;
  }

  /**
   * Resolves when the socket is OPEN, not when the constructor returns.
   *
   * The previous version resolved as soon as `new WebSocket()` had been called,
   * so `await connect()` succeeded against an endpoint that was refusing the
   * connection, and startup reported itself healthy while nothing was
   * listening. Rejecting here is what lets scheduleReconnect() back off
   * properly instead of spinning against a dead endpoint.
   */
  async connect({ minTtlMs } = {}) {
    const token = await getAccessToken(minTtlMs ? { minTtlMs } : undefined);
    if (!token) throw new Error('Not authorized yet — complete the OAuth flow first.');

    const base = config.wsEndpoint;
    const url = `${base}${base.includes('?') ? '&' : '?'}access_token=${token}`;

    // Anything still open is now superseded. Leaving it open leaked a socket
    // per reconnect and Zoom eventually answers a client holding several.
    this.retireCurrentSocket();

    this.intentionalClose = false;
    const ws = new WebSocket(url);
    this.ws = ws;

    return new Promise((resolve, reject) => {
      let settled = false;

      ws.on('open', () => {
        if (ws !== this.ws) return;
        this.attempts = 0;
        console.log('[events] connected to Zoom event socket');
        this.startPing();
        this.scheduleTokenRecycle();
        if (!settled) { settled = true; resolve(); }
      });

      ws.on('message', (raw) => {
        if (ws !== this.ws) return;
        this.handleMessage(raw);
      });

      ws.on('close', (code, reason) => {
        // A superseded socket must not touch shared state. Without this guard
        // the OLD socket's close handler runs after connect() has already
        // installed a NEW socket, and stopTimers() then kills the NEW
        // keep-alive and recycle timers while scheduleReconnect() opens a
        // second connection.
        if (ws !== this.ws) return;
        console.log(`[events] closed: ${code} ${reason?.toString() ?? ''}`);
        this.stopTimers();

        // Closed before it ever opened: the awaiting caller must hear about it.
        if (!settled) {
          settled = true;
          reject(new Error(`event socket closed before opening (${code})`));
          if (!this.intentionalClose && !this.recycling) this.scheduleReconnect();
          return;
        }

        if (this.recycling) {
          // Planned token recycle: reconnect now that the socket is genuinely
          // closed, rather than racing its close event.
          this.recycling = false;
          this.connect({ minTtlMs: RECYCLE_MIN_TTL_MS }).catch((err) => {
            console.error('[events] token-recycle reconnect failed:', err.message);
            this.scheduleReconnect();
          });
          return;
        }
        if (!this.intentionalClose) this.scheduleReconnect();
      });

      ws.on('error', (err) => {
        if (ws !== this.ws) return;
        console.error('[events] socket error:', err.message);
        // 'close' fires next; reconnect and rejection are handled there.
      });
    });
  }

  /** Detach and close whatever socket we were holding, without its handlers
   *  reaching back into state that now belongs to a newer connection. */
  retireCurrentSocket() {
    const old = this.ws;
    if (!old) return;
    this.ws = null;              // every handler's `ws !== this.ws` guard now trips
    this.stopTimers();
    try {
      if (old.readyState === WebSocket.OPEN || old.readyState === WebSocket.CONNECTING) {
        old.close(1000);
      }
    } catch { /* already gone */ }
  }

  handleMessage(raw) {
    let msg;
    try {
      msg = JSON.parse(raw.toString());
    } catch {
      return;
    }

    // Zoom's WS frames: connection acks and heartbeats use "module";
    // real events arrive as {module:"message", content:"<stringified event>"}.
    if (msg.module === 'build_connection') {
      console.log(`[events] build_connection success=${msg.success}`);
      return;
    }
    if (msg.module === 'heartbeat') return;

    let evt = msg;
    if (msg.module === 'message' && msg.content) {
      try {
        evt = JSON.parse(msg.content);
      } catch {
        return;
      }
    }
    if (evt?.event) {
      console.log(`[events] ${evt.event}`);
      Promise.resolve()
        .then(() => this.onEvent(evt))
        .catch((err) => console.error('[events] handler error:', err));
    }
  }

  startPing() {
    clearInterval(this.pingTimer);
    this.pingTimer = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.ping();
        try {
          this.ws.send(JSON.stringify({ module: 'heartbeat' }));
        } catch {
          /* socket mid-close; reconnect handles it */
        }
      }
    }, PING_INTERVAL_MS);
  }

  stopTimers() {
    clearInterval(this.pingTimer);
    clearTimeout(this.recycleTimer);
    this.pingTimer = null;
    this.recycleTimer = null;
  }

  /** Access tokens last 1 hour — proactively reconnect with a fresh one. */
  scheduleTokenRecycle() {
    clearTimeout(this.recycleTimer);
    this.recycleTimer = setTimeout(() => {
      console.log('[events] recycling connection with a fresh access token');
      // Hand the reconnect to the close handler. Calling connect() here races
      // the old socket's close event and produces two sockets with no timers.
      this.recycling = true;
      this.intentionalClose = true;
      this.ws?.close(1000);
    }, TOKEN_RECYCLE_MS);
  }

  scheduleReconnect() {
    const delay = Math.min(1000 * 2 ** this.attempts++, MAX_BACKOFF_MS);
    console.log(`[events] reconnecting in ${Math.round(delay / 1000)}s (attempt ${this.attempts})`);
    setTimeout(() => {
      this.connect().catch((err) => {
        console.error('[events] reconnect failed:', err.message);
        this.scheduleReconnect();
      });
    }, delay);
  }

  close() {
    this.intentionalClose = true;
    // Clear a pending recycle too, or SIGINT during the recycle window would
    // close the socket and then dutifully reconnect it on the way out.
    this.recycling = false;
    this.stopTimers();
    const ws = this.ws;
    this.ws = null;
    try { ws?.close(1000); } catch { /* already gone */ }
  }
}
