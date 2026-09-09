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

// A single bound on the WHOLE of connect(): TCP, the WebSocket upgrade, and
// Zoom's build_connection acknowledgement.
//
// The first version of this timer started on the `open` event, which left the
// preceding window unguarded — and that window has its own way of hanging. If an
// endpoint or an intermediary accepts the TCP connection but never completes the
// upgrade, `open` never fires and neither does `close`, so the timer was never
// created at all and connect() stayed pending forever with nothing scheduling a
// retry. That is the same dead agent the timeout exists to prevent, reached
// through the door I had not covered.
//
// One timer, started the moment the socket is constructed, cannot have that gap.
// Default lives in config.js so a test can exercise the hang without waiting.

// Belt and braces on the upgrade specifically: `ws` fails the handshake itself
// rather than relying on our timer to notice.
const HANDSHAKE_TIMEOUT_MS = 10_000;

// Exported ONLY so a test can assert the horizon the recycle actually asks
// for. A mutation control reduced this constant back to five minutes -- the
// exact defect blocker 5 was about -- and the suite stayed green, because the
// existing test called getAccessToken({minTtlMs}) itself and so proved the
// oauth side honours a horizon without ever checking WHICH horizon the
// recycle passes. The fix was covered; the wiring to it was not.
export const __recycleContract = { TOKEN_RECYCLE_MS, RECYCLE_MIN_TTL_MS };

export class ZoomEventSocket {
  constructor(onEvent) {
    this.onEvent = onEvent;
    this.ws = null;
    this.pingTimer = null;
    this.recycleTimer = null;
    this.reconnectTimer = null;
    this.attempts = 0;
    this.intentionalClose = false;
    this.recycling = false;
    this.ackFailed = false;
    this.onAck = null;
  }

  /**
   * Resolves only after Zoom ACKNOWLEDGES the connection, not when the socket
   * opens and certainly not when the constructor returns.
   *
   * Two failures, found one review apart, both of which looked like health:
   *
   *   1. The first version resolved as soon as `new WebSocket()` had been
   *      called, so `await connect()` succeeded against an endpoint that was
   *      refusing the connection.
   *   2. The second resolved on the transport `open` event -- but Zoom answers
   *      with `{module:'build_connection', success:false}` when it declines the
   *      subscription, and that was only logged. The agent then sat there
   *      looking connected, scheduling no retry and no reauthorization, and
   *      received no meeting events at all until the 50-minute recycle.
   *
   * A TCP handshake is not a subscription. Readiness is what Zoom says it is.
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
    const ws = new WebSocket(url, { handshakeTimeout: HANDSHAKE_TIMEOUT_MS });
    this.ws = ws;

    return new Promise((resolve, reject) => {
      let settled = false;

      const settle = (fn, value) => {
        if (settled) return;
        settled = true;
        clearTimeout(connectTimer);
        fn(value);
      };

      // Started NOW, not on open. Covers every way this can hang.
      const connectTimer = setTimeout(() => {
        if (ws !== this.ws || settled) return;
        console.error(
          `[events] no acknowledgement within ${config.connectTimeoutMs / 1000}s `
          + `(state=${ws.readyState}) — closing`,
        );
        this.ackFailed = true;
        try { ws.close(1000); } catch { /* already gone */ }
        // A socket stuck mid-upgrade may never emit close either, so the
        // rejection cannot wait for it.
        setTimeout(() => {
          if (settled) return;
          const err = new Error('Zoom did not acknowledge the connection');
          if (!this.intentionalClose && !this.recycling) {
            this.scheduleReconnect();
            err.retryScheduled = true;
          }
          settle(reject, err);
        }, 250).unref?.();
      }, config.connectTimeoutMs);
      connectTimer.unref?.();

      // Called by handleMessage when the acknowledgement frame arrives.
      this.onAck = (success) => {
        if (ws !== this.ws) return;
        if (success) {
          this.attempts = 0;
          // A BACKOFF ARMED BEFORE THIS SUCCEEDED IS NOW A LIABILITY, NOT A
          // SAFETY NET. It was scheduled for a failure that has since been
          // superseded — most plausibly a pre-socket failure ("Not authorized
          // yet") whose backoff was still counting down while the operator
          // finished the OAuth flow, and the callback connected for real. When
          // that stale timer then fires it calls connect(), which retires this
          // healthy acknowledged socket and rebuilds it: a window with no event
          // subscription at all, and any meeting.rtms_started arriving inside it
          // is simply not received. A retry that tears down the thing it was
          // meant to restore.
          this.clearPendingReconnect();
          console.log('[events] Zoom acknowledged the connection — listening');
          this.startPing();
          this.scheduleTokenRecycle();
          settle(resolve);
          return;
        }
        // Declined. Close it rather than sit on a socket that will never
        // deliver an event, and let the close handler own the retry.
        console.error('[events] Zoom REFUSED the connection (build_connection success=false) — closing');
        this.ackFailed = true;
        try { ws.close(1000); } catch { /* already gone */ }
      };

      ws.on('open', () => {
        if (ws !== this.ws) return;
        console.log('[events] socket open — waiting for Zoom to acknowledge');
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

        // Closed before it was acknowledged: the awaiting caller must hear about
        // it. This covers a refused build_connection and a silent server, not
        // only a socket that never opened.
        //
        // EXACTLY ONE OWNER SCHEDULES THE RETRY. This handler schedules, and
        // marks the rejection so the caller's catch does not schedule a SECOND
        // one for the same failure. Without the marker each generation created
        // two timers, then four, and during a sustained outage the recovered
        // connection kept being superseded by delayed attempts from earlier
        // generations. Introduced by making connect() reject on a pre-open
        // close, which is the fix that made the double path possible.
        if (!settled) {
          const why = this.ackFailed
            ? 'Zoom did not acknowledge the connection'
            : `event socket closed before opening (${code})`;
          this.ackFailed = false;
          const err = new Error(why);
          if (!this.intentionalClose && !this.recycling) {
            this.scheduleReconnect();
            err.retryScheduled = true;
          }
          settle(reject, err);
          return;
        }

        if (this.recycling) {
          // Planned token recycle: reconnect now that the socket is genuinely
          // closed, rather than racing its close event.
          this.recycling = false;
          this.connect({ minTtlMs: RECYCLE_MIN_TTL_MS }).catch((err) => {
            console.error('[events] token-recycle reconnect failed:', err.message);
            if (!err.retryScheduled) this.scheduleReconnect();
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
      // The acknowledgement decides readiness. success=false used to be logged
      // and otherwise ignored, which left the agent looking connected while
      // Zoom had declined it.
      console.log(`[events] build_connection success=${msg.success}`);
      this.onAck?.(Boolean(msg.success));
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

  /** Drop a backoff that is no longer wanted. Safe to call when none is armed.
   *
   *  NOT folded into stopTimers(). The close handler calls stopTimers() and
   *  then scheduleReconnect(), so it would survive today — but only because of
   *  that order, and an edit that swapped the two lines would silently disable
   *  every retry in the class with no test able to see the difference. The two
   *  socket-lifetime timers and the between-sockets backoff have genuinely
   *  different lifetimes; keeping them apart is what says so. */
  clearPendingReconnect() {
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
  }

  scheduleReconnect() {
    // Never let two backoffs run at once: without this, a failure arriving
    // while one is already counting down doubles the attempts, and the pattern
    // compounds across generations.
    this.clearPendingReconnect();
    const delay = Math.min(1000 * 2 ** this.attempts++, MAX_BACKOFF_MS);
    console.log(`[events] reconnecting in ${Math.round(delay / 1000)}s (attempt ${this.attempts})`);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect().catch((err) => {
        console.error('[events] reconnect failed:', err.message);
        // Only schedule when nothing else did. A failure that never produced a
        // socket -- an unauthorized getAccessToken, say -- has no close handler
        // to own the retry, so this caller must.
        if (!err.retryScheduled) this.scheduleReconnect();
      });
    }, delay);
    this.reconnectTimer.unref?.();
  }

  close() {
    this.intentionalClose = true;
    // A shutdown that leaves a backoff armed reconnects on the way out.
    this.clearPendingReconnect();
    // Clear a pending recycle too, or SIGINT during the recycle window would
    // close the socket and then dutifully reconnect it on the way out.
    this.recycling = false;
    this.stopTimers();
    const ws = this.ws;
    this.ws = null;
    try { ws?.close(1000); } catch { /* already gone */ }
  }
}
