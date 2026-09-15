#!/usr/bin/env python3
"""codex-switchboard — one Codex engine, several ChatGPT accounts, swapped underneath it.

Why this exists: every account hits its weekly limit at a different moment, and the usual
fix is to quit the TUI and relaunch it under another CODEX_HOME. Codex 0.154's app-server has a login mode, `chatgptAuthTokens`, in which the credential is
handed to it by a client and can be handed over again at any time. The threads, the
context and the TUI attached to it all stay put. This script is that client.

Pieces:
  engine   `codex app-server --listen unix://` under the `pool` profile (no auth.json of its
           own; config/history are symlinked to ~/.codex by `codex-acct reconcile pool`).
  run      the controller loop: logs the engine in with the first account that the backend
           says may still run, re-checks every PROACTIVE_S, and swaps the account the
           moment the current one is refused. A turn that still fails on the limit is
           followed by a queued continuation in the same thread.
  TUI      `codex --remote unix://` with CODEX_HOME=pool (the `codex` shell function in
           the README).

Tokens: each profile keeps refreshing through Codex's own code path — a helper
`codex app-server` under that profile's CODEX_HOME with `account/read {refreshToken:true}`
— so auth.json stays single-copy per machine (the openai/codex#19803 rule in codex-acct).

Commands:
  switchboard.py ensure          start engine + controller if not running; print account
  switchboard.py status          current account, limits, engine pid, last events
  switchboard.py switch <name>   force an account now (the loop still moves off it when refused)
  switchboard.py stop            stop controller and engine
  switchboard.py run             controller loop in the foreground (what `ensure` detaches)
  switchboard.py pick            print the CODEX_HOME of the best account right now (for
                                 `codex exec` jobs, which cannot attach to the engine)

Accounts: ~/.codex-profiles/pool/order lists the pool, one profile name per line, first
preferred (`default` is ~/.codex). Without the file the pool is `default` plus every
profile under ~/.codex-profiles that has an auth.json, in name order.

Alerts: if ~/.codex-profiles/pool/alert-webhook holds a webhook URL (Slack- or
Feishu-style incoming webhook), the controller posts there when every account is
exhausted and again when one is back.

Logs: ~/.codex-profiles/pool/switchboard.log (controller), engine.log (app-server stderr).
State: ~/.codex-profiles/pool/switchboard-state.json, rewritten every tick.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path

PROFILE_ROOT = Path(os.environ.get("CODEX_ACCT_ROOT", Path.home() / ".codex-profiles"))
POOL = PROFILE_ROOT / "pool"
ORDER_FILE = POOL / "order"               # one profile name per line, first preferred
SOCK = POOL / "app-server-control" / "app-server-control.sock"
LOG = POOL / "switchboard.log"
ENGINE_LOG = POOL / "engine.log"
STATE = POOL / "switchboard-state.json"
PID = POOL / "switchboard.pid"
SWITCH_REQUEST = POOL / "switch-request"
ALERT_WEBHOOK = POOL / "alert-webhook"    # incoming-webhook URL, 0600; absent = no alerts
ALERT_REPEAT_S = 6 * 3600                 # re-alert while still exhausted at most this often



def load_order() -> list[str]:
    try:
        names = [l.strip() for l in ORDER_FILE.read_text().splitlines()]
        names = [n for n in names if n and not n.startswith("#")]
        if names:
            return names
    except OSError:
        pass
    found = sorted(d.name for d in PROFILE_ROOT.iterdir()
                   if d.is_dir() and d.name != "pool" and (d / "auth.json").is_file()) if PROFILE_ROOT.is_dir() else []
    return ["default"] + found


ORDER = load_order()

# Transport, measured on codex-cli 0.154.0. Over the Responses websocket a thread
# keeps the backend connection it opened on its first turn, and that connection carries the
# credential current at that moment; an account swap on the engine does not reach it (not
# after logout+login, a re-login, thread/resume, compaction or 200 s idle). Two things do
# reach it: a brand-new thread, which connects with the current account; and a turn that
# fails on the usage limit, which closes the connection, so the next turn reconnects with
# the current account. That is enough for the design below (switch early for new threads,
# continue after the one failed turn for existing ones). Plain HTTP would avoid the pin but
# re-sends the whole context on every model call: about +1 s a call with 80 KB of context,
# paid once per tool step — too slow, so the websocket stays.
ENGINE_PROVIDER_OVERRIDES: list[str] = []

PROACTIVE_S = 30      # ask the backend about the current account this often
# Leave an account once its weekly window has less than this much left, provided another
# account in the pool has at least this much. The switch then happens between turns and no
# turn ever hits the wall; the reactive path below stays as the backstop. What is left
# below the line is not lost: when every other account is out, the picker comes back to it.
SWITCH_BELOW_PERCENT = 5
REACTIVE_S = 3        # look for turns that failed on the limit this often
REFRESH_AHEAD_S = 24 * 3600   # refresh a token this long before its expiry
CONTINUATION = (
    "The previous turn stopped because the Codex usage limit was reached and the account "
    "has been switched. Continue the unfinished work from that turn in this same "
    "conversation, checking what already ran before repeating any action. Keep the existing "
    "scope, model, and permissions. Stop when the original request is complete or requires "
    "user input."
)


def log(event: str, **fields) -> None:
    line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields})
    with LOG.open("a") as f:
        f.write(line + "\n")


def alert(text: str) -> None:
    """One message to the incoming webhook, if one is configured. The payload carries both
    the Slack shape (`text`) and the Feishu/Lark shape (`msg_type` + `content`)."""
    try:
        url = ALERT_WEBHOOK.read_text().strip()
    except OSError:
        return
    if not url:
        return
    import urllib.request
    text = f"[codex-switchboard @ {socket.gethostname()}] {text}"
    body = json.dumps({"text": text, "msg_type": "text", "content": {"text": text}}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            log("alert_sent", status=r.status, text=text)
    except Exception as e:
        log("alert_failed", error=str(e)[:200], text=text)


def profile_home(name: str) -> Path:
    return Path.home() / ".codex" if name == "default" else PROFILE_ROOT / name


def jwt_claims(token: str) -> dict:
    part = token.split(".")[1]
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


def read_tokens(name: str) -> dict | None:
    path = profile_home(name) / "auth.json"
    if not path.is_file():
        return None
    tokens = json.load(path.open()).get("tokens") or {}
    access = tokens.get("access_token")
    if not access:
        return None
    claims = jwt_claims(access)
    idc = jwt_claims(tokens["id_token"]) if tokens.get("id_token") else {}
    auth = idc.get("https://api.openai.com/auth", {})
    return {
        "access_token": access,
        "account_id": tokens.get("account_id") or auth.get("chatgpt_account_id"),
        "plan": auth.get("chatgpt_plan_type"),
        "email": idc.get("email"),
        "exp": claims.get("exp", 0),
    }


# ---------------------------------------------------------------- helper app-server (per profile)

def helper_call(name: str, method: str, params: dict | None = None, timeout: float = 45) -> dict:
    """One JSON-RPC call on a throwaway `codex app-server` under that profile's CODEX_HOME.
    This is how a profile's own credential gets used and refreshed by Codex's own code."""
    env = dict(os.environ, CODEX_HOME=str(profile_home(name)))
    # `codex` is a node wrapper that spawns the native binary; kill the whole process group or
    # the native app-server outlives every probe.
    p = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, env=env, text=True, start_new_session=True)
    try:
        def send(o):
            p.stdin.write(json.dumps(o) + "\n")
            p.stdin.flush()
        send({"id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "codex-switchboard", "version": "1"}}})
        send({"method": "initialized"})
        send({"id": 2, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = p.stdout.readline()
            if not line:
                break
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("id") == 2:
                if "error" in m:
                    raise RuntimeError(f"{method} on {name}: {json.dumps(m['error'])[:200]}")
                return m["result"]
        raise TimeoutError(f"{method} on {name}: no reply in {timeout}s")
    finally:
        os.killpg(p.pid, signal.SIGKILL)


def probe(name: str) -> dict | None:
    """What the backend says about this account right now. None = could not ask."""
    if read_tokens(name) is None:
        log("probe_skip", account=name, reason="no auth.json")
        LAST_PROBE[name] = None
        return None
    try:
        r = helper_call(name, "account/rateLimits/read")
    except Exception as e:  # network, revoked refresh token, ...
        log("probe_error", account=name, error=str(e)[:200])
        LAST_PROBE[name] = None
        return None
    limits = r.get("rateLimits") or {}
    primary = limits.get("primary") or {}
    out = {
        "allowed": r.get("ordinaryUsageAllowed"),
        "reached": limits.get("rateLimitReachedType"),
        "used": primary.get("usedPercent"),
        "resets_at": primary.get("resetsAt"),
    }
    log("probe", account=name, **out)
    LAST_PROBE[name] = out
    if out["resets_at"]:
        RESET_TIMES[name] = out["resets_at"]
    return out


RESET_TIMES: dict[str, int] = {}   # account -> weekly window reset, from the last probe
LAST_PROBE: dict[str, dict | None] = {}   # account -> last probe result; None = could not authenticate


def usable(p: dict | None) -> bool:
    # The backend's own verdict. usedPercent can read 100 while a turn is still allowed, and
    # `allowed` flips false only after the last one is spent — so it is the flag, not the number.
    # `allowed` is None when the backend's permission lookup was unavailable; then
    # rateLimitReachedType is the only signal left.
    if not p or p["reached"]:
        return False
    return p["allowed"] is not False


def remaining(p: dict | None) -> int:
    used = (p or {}).get("used")
    return 100 - used if isinstance(used, int) else 0


def roomy(p: dict | None) -> bool:
    return usable(p) and remaining(p) >= SWITCH_BELOW_PERCENT


def fresh_token(name: str, force: bool = False) -> dict:
    t = read_tokens(name)
    if t is None:
        raise RuntimeError(f"{name}: no credential (codex-acct login {name})")
    if force or t["exp"] - time.time() < REFRESH_AHEAD_S:
        log("refresh", account=name, force=force, exp_in_h=round((t["exp"] - time.time()) / 3600, 1))
        helper_call(name, "account/read", {"refreshToken": True})
        t = read_tokens(name) or t
    return t


def pick(exclude: set[str] = frozenset()) -> tuple[str | None, dict | None]:
    """First account in ORDER with headroom; failing that, the first the backend still allows."""
    probes: list[tuple[str, dict | None]] = []
    for name in ORDER:
        if name in exclude:
            continue
        p = probe(name)
        if roomy(p):
            return name, p
        probes.append((name, p))
    for name, p in probes:
        if usable(p):
            return name, p
    return None, None


# ---------------------------------------------------------------- engine (the shared app-server)

def process_env(pid: str) -> str:
    if sys.platform == "linux":
        try:
            return Path(f"/proc/{pid}/environ").read_bytes().replace(b"\0", b"\n").decode(errors="replace")
        except OSError:
            return ""
    return subprocess.run(["ps", "-E", "-o", "command=", "-p", pid], capture_output=True, text=True).stdout


def engine_pid() -> int | None:
    """The native engine process (the node wrapper that spawned it may already be gone).
    Found by its command line plus CODEX_HOME, so other app-servers on the machine are skipped."""
    out = subprocess.run(["pgrep", "-f", "codex app-server --listen unix://"],
                         capture_output=True, text=True).stdout.split()
    found = [int(pid) for pid in out if f"CODEX_HOME={POOL}" in process_env(pid)]
    return max(found) if found else None      # the native child, when the wrapper is still there


def engine_reachable() -> bool:
    try:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(2)
        s.connect(str(SOCK))
        s.close()
        return True
    except OSError:
        return False


def start_engine() -> int:
    subprocess.run(["codex-acct", "reconcile", "pool"], check=True)
    if (POOL / "auth.json").exists():
        raise RuntimeError(f"{POOL}/auth.json exists; the pool must hold no stored credential")
    p = subprocess.Popen(["codex", "app-server", "--listen", "unix://", *ENGINE_PROVIDER_OVERRIDES],
                         env=dict(os.environ, CODEX_HOME=str(POOL)),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=ENGINE_LOG.open("a"), start_new_session=True)
    for _ in range(100):
        if engine_reachable():
            log("engine_started", pid=p.pid)
            return p.pid
        if p.poll() is not None:
            break
        time.sleep(0.15)
    raise RuntimeError(f"engine did not come up; see {ENGINE_LOG}")


# ---------------------------------------------------------------- websocket over the unix socket

class Ws:
    """Minimal RFC 6455 client: the engine speaks WebSocket over its unix socket, path /rpc."""

    async def connect(self):
        self.r, self.w = await asyncio.open_unix_connection(str(SOCK))
        key = base64.b64encode(os.urandom(16)).decode()
        self.w.write((f"GET /rpc HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                      f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
        await self.w.drain()
        head = await self.r.readuntil(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError(f"handshake refused: {head[:120]!r}")
        return self

    async def send(self, text: str):
        b = text.encode()
        n = len(b)
        if n < 126:
            head = bytes([0x81, 0x80 | n])
        elif n < 65536:
            head = bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
        else:
            head = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.w.write(head + mask + bytes(x ^ mask[i % 4] for i, x in enumerate(b)))
        await self.w.drain()

    async def recv(self) -> str:
        buf = b""
        while True:
            h = await self.r.readexactly(2)
            fin, op, n = h[0] & 0x80, h[0] & 0x0F, h[1] & 0x7F
            if n == 126:
                n = struct.unpack(">H", await self.r.readexactly(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", await self.r.readexactly(8))[0]
            data = await self.r.readexactly(n)
            if h[1] & 0x80:
                mask = await self.r.readexactly(4)
                data = bytes(x ^ mask[i % 4] for i, x in enumerate(data))
            if op == 0x9:      # ping -> pong
                self.w.write(bytes([0x8A, 0x80]) + os.urandom(4))
                await self.w.drain()
                continue
            if op == 0x8:
                raise EOFError("engine closed the connection")
            if op in (0x1, 0x0):
                buf += data
                if fin:
                    return buf.decode()

    def close(self):
        self.w.close()


class Engine:
    """JSON-RPC over the websocket, with server->client requests routed to a handler."""

    def __init__(self, on_server_request):
        self.ws = Ws()
        self.next_id = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.on_server_request = on_server_request
        self.reader: asyncio.Task | None = None

    async def open(self):
        await self.ws.connect()
        self.reader = asyncio.create_task(self._read())
        await self.call("initialize", {"clientInfo": {"name": "codex-switchboard", "version": "1"},
                                       "capabilities": {"experimentalApi": True}})
        await self.ws.send(json.dumps({"method": "initialized"}))

    async def _read(self):
        try:
            while True:
                m = json.loads(await self.ws.recv())
                if "id" in m and "method" in m:           # request from the engine
                    asyncio.create_task(self.on_server_request(self, m))
                elif "id" in m:
                    fut = self.pending.pop(m["id"], None)
                    if fut and not fut.done():
                        fut.set_result(m)
                elif m.get("method") == "account/updated":
                    log("engine_account_updated", **(m.get("params") or {}))
        except Exception as e:
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(e)
            self.pending.clear()

    async def call(self, method: str, params: dict | None = None, timeout: float = 60):
        self.next_id += 1
        i = self.next_id
        fut = asyncio.get_running_loop().create_future()
        self.pending[i] = fut
        await self.ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
        m = await asyncio.wait_for(fut, timeout)
        if "error" in m:
            raise RuntimeError(f"{method}: {json.dumps(m['error'])[:300]}")
        return m.get("result")

    async def respond(self, req_id, result: dict):
        await self.ws.send(json.dumps({"id": req_id, "result": result}))

    def dead(self) -> bool:
        return self.reader is not None and self.reader.done()


# ---------------------------------------------------------------- controller

class Controller:
    def __init__(self):
        self.current: str | None = None
        self.since: float = 0
        self.handled_turns: set[str] = set()
        self.unreadable_threads: set[str] = set()   # logged once, then skipped
        self.exhausted_since: float | None = None
        self.last_alert: float = 0
        self.last_limits: dict | None = None
        self.engine: Engine | None = None
        self.engine_pid: int | None = None
        self.started_at = time.time()

    def write_state(self, **extra):
        STATE.write_text(json.dumps({
            "account": self.current, "since": self.since, "limits": self.last_limits,
            "engine_pid": self.engine_pid, "controller_pid": os.getpid(),
            "updated": time.time(), **extra}, indent=1))

    async def login(self, name: str, reason: str):
        t = await asyncio.to_thread(fresh_token, name)
        await self.engine.call("account/login/start", {
            "type": "chatgptAuthTokens", "accessToken": t["access_token"],
            "chatgptAccountId": t["account_id"], "chatgptPlanType": t["plan"]})
        prev = self.current
        self.current, self.since = name, time.time()
        log("switch", account=name, email=t["email"], previous=prev, reason=reason)
        self.write_state()

    async def on_server_request(self, engine: Engine, m: dict):
        if m["method"] == "account/chatgptAuthTokens/refresh":
            # The engine got a 401 on the current token: refresh it through the profile's own
            # codex path and hand back the new one. 10s budget on the engine side.
            log("engine_refresh_request", **(m.get("params") or {}))
            try:
                t = await asyncio.to_thread(fresh_token, self.current, True)
                await engine.respond(m["id"], {"accessToken": t["access_token"],
                                               "chatgptAccountId": t["account_id"],
                                               "chatgptPlanType": t["plan"]})
            except Exception as e:
                log("engine_refresh_failed", error=str(e)[:200])
        else:
            log("engine_request_ignored", method=m["method"])

    async def choose_and_login(self, reason: str, exclude: set[str] = frozenset()):
        name, p = await asyncio.to_thread(pick, exclude)
        if name is None:
            # Nothing usable: stay logged in as an account that at least authenticates, so the
            # TUI can still work (its own Luna Reserve fallback, if the account has one), and
            # say so loudly. An account whose probe errored (revoked token) is never the fallback.
            alive = [n for n in ORDER if n not in exclude and isinstance(LAST_PROBE.get(n), dict)]
            fallback = (self.current if self.current in alive else None) or (alive[0] if alive else None)
            log("all_exhausted", fallback=fallback, reason=reason)
            if fallback and fallback != self.current:
                await self.login(fallback, f"{reason}; all accounts exhausted")
            self.write_state(all_exhausted=True)
            now = time.time()
            if self.exhausted_since is None or now - self.last_alert >= ALERT_REPEAT_S:
                self.exhausted_since = self.exhausted_since or now
                self.last_alert = now
                resets = sorted(t for t in RESET_TIMES.values() if t)
                nxt = time.strftime("%m-%d %H:%M", time.localtime(resets[0])) if resets else "?"
                await asyncio.to_thread(alert, f"every account in the pool is out of quota; earliest reset {nxt} ({reason})")
            return False
        if self.exhausted_since is not None:
            self.exhausted_since = None
            await asyncio.to_thread(alert, f"quota is back: switched to {name} ({remaining(p)}% left)")
        if name != self.current:
            await self.login(name, reason)
        self.last_limits = p
        self.write_state()
        return True

    async def confirm_live(self, name: str) -> bool:
        """Round-trip to the backend through the engine with the account we just handed it, and
        check the backend names that account. Proves the swap is in effect before we rely on it."""
        want = (read_tokens(name) or {}).get("account_id")
        for _ in range(5):
            try:
                r = await self.engine.call("account/rateLimits/read", timeout=40)
            except Exception as e:
                log("confirm_live_error", account=name, error=str(e)[:200])
                await asyncio.sleep(1)
                continue
            got = r.get("accountId")
            if got == want:
                log("confirm_live", account=name, account_id=got)
                return True
            log("confirm_live_mismatch", account=name, want=want, got=got)
            await asyncio.sleep(1)
        return False

    async def current_limits(self) -> dict | None:
        try:
            r = await self.engine.call("account/rateLimits/read", timeout=40)
        except Exception as e:
            log("limits_error", error=str(e)[:200])
            return None
        limits = r.get("rateLimits") or {}
        primary = limits.get("primary") or {}
        p = {"allowed": r.get("ordinaryUsageAllowed"), "reached": limits.get("rateLimitReachedType"),
             "used": primary.get("usedPercent"), "resets_at": primary.get("resetsAt")}
        self.last_limits = p
        self.write_state()
        return p

    async def proactive(self):
        if not self.current:
            return
        p = await self.current_limits()
        if p is None:
            # The engine cannot even read limits on this account: revoked or unreachable.
            # Re-probe the pool and move; a dead account must not stay in the engine.
            log("current_unreadable", account=self.current)
            await self.choose_and_login("engine cannot read limits on current account", exclude={self.current})
            return
        if not usable(p):
            log("current_refused", account=self.current, **p)
            await self.choose_and_login("backend refuses current account", exclude={self.current})
        elif remaining(p) < SWITCH_BELOW_PERCENT:
            # Still allowed, but nearly out: move early if someone else has room. pick() falls
            # back to any allowed account, so guard against bouncing to one that is no better.
            name, cand = await asyncio.to_thread(pick, {self.current})
            if name and roomy(cand):
                log("current_low", account=self.current, remaining=remaining(p), next=name)
                await self.login(name, f"only {remaining(p)}% left, {name} has {remaining(cand)}%")
                self.last_limits = cand
                self.write_state()

    async def reactive(self):
        """A turn that failed on the limit: switch, then queue a continuation in that thread."""
        # `thread/loaded/list` only lists threads loaded by *this* connection, so the TUI's are
        # invisible there. The shared thread table sees all of them; anything touched in the last
        # few minutes is a candidate.
        try:
            threads = (await self.engine.call("thread/list", {"limit": 20}, timeout=20)).get("data") or []
        except Exception as e:
            log("reactive_error", error=str(e)[:200])
            return
        # `codex exec` threads are skipped: that process exits on the failure, so nobody would
        # pick up a queued continuation. Such jobs choose their account up front with `pick`.
        recent = [t["id"] for t in threads
                  if (t.get("updatedAt") or 0) >= time.time() - 600 and t.get("source") != "exec"]
        for tid in recent:
            try:
                turns = (await self.engine.call("thread/turns/list",
                                                {"threadId": tid, "limit": 1, "itemsView": "notLoaded"},
                                                timeout=20)).get("data") or []
            except Exception as e:
                if tid not in self.unreadable_threads:
                    self.unreadable_threads.add(tid)
                    log("reactive_error", thread=tid, error=str(e)[:200])
                continue
            if not turns:
                continue
            turn = turns[0]
            err = turn.get("error") or {}
            info = err.get("codexErrorInfo")
            info = info if isinstance(info, str) else (info or {}).get("type")
            if turn.get("status") != "failed" or info != "usageLimitExceeded":
                continue
            if turn["id"] in self.handled_turns:
                continue
            if (turn.get("completedAt") or 0) < self.started_at:
                continue      # failed before this controller existed: not ours to continue
            self.handled_turns.add(turn["id"])
            log("turn_failed_on_limit", thread=tid, turn=turn["id"], account=self.current)
            # The thread may still have been on an account we already left (its websocket
            # pins the credential of its first turn); the failure closed that connection, so
            # the next turn reconnects with whatever the engine holds now. Only switch if the
            # engine's current account is itself refused.
            cur = await self.current_limits()
            if cur is None or not usable(cur):
                if not await self.choose_and_login("turn failed on usage limit", exclude={self.current}):
                    continue
                if not await self.confirm_live(self.current):
                    log("continuation_skipped", thread=tid, reason="new account not confirmed live on the engine")
                    continue
            else:
                log("current_still_usable", account=self.current, remaining=remaining(cur))
            try:
                r = await self.engine.call("thread/queue/add", {
                    "threadId": tid, "clientUserMessageId": str(uuid.uuid4()),
                    "input": [{"type": "text", "text": CONTINUATION}]}, timeout=20)
                log("continuation_queued", thread=tid, queued=(r or {}).get("queuedSubmission", {}).get("id"))
            except Exception as e:
                log("continuation_failed", thread=tid, error=str(e)[:200])

    async def manual_switch(self):
        if not SWITCH_REQUEST.exists():
            return
        name = SWITCH_REQUEST.read_text().strip()
        SWITCH_REQUEST.unlink()
        if name not in ORDER:
            log("manual_switch_rejected", account=name)
            return
        try:
            await self.login(name, "manual switch")
        except Exception as e:
            log("manual_switch_failed", account=name, error=str(e)[:200])

    async def connect_engine(self):
        if not engine_reachable():
            self.engine_pid = await asyncio.to_thread(start_engine)
        else:
            self.engine_pid = engine_pid()
        self.engine = Engine(self.on_server_request)
        await self.engine.open()
        log("engine_connected", pid=self.engine_pid)
        self.current = None      # a fresh engine has no account until we hand it one
        await self.choose_and_login("startup")

    async def run(self):
        PID.write_text(str(os.getpid()))
        log("controller_start", pid=os.getpid(), order=ORDER)
        await self.connect_engine()
        last_proactive = time.time()
        while True:
            await asyncio.sleep(REACTIVE_S)
            if self.engine.dead():
                log("engine_lost")
                self.engine.ws.close()
                await asyncio.sleep(1)
                try:
                    await self.connect_engine()
                except Exception as e:
                    log("engine_restart_failed", error=str(e)[:200])
                    await asyncio.sleep(10)
                continue
            await self.manual_switch()
            await self.reactive()
            if time.time() - last_proactive >= PROACTIVE_S:
                last_proactive = time.time()
                await self.proactive()


# ---------------------------------------------------------------- commands

def pid_alive(path: Path) -> int | None:
    try:
        pid = int(path.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def read_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def cmd_ensure() -> int:
    POOL.mkdir(parents=True, exist_ok=True)
    pid = pid_alive(PID)
    if pid and engine_reachable() and read_state().get("account"):
        st = read_state()
        print(f"codex-switchboard: {st['account']} (engine pid {st.get('engine_pid')})", file=sys.stderr)
        return 0
    if pid:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
    STATE.unlink(missing_ok=True)
    subprocess.Popen([sys.executable, __file__, "run"], stdin=subprocess.DEVNULL,
                     stdout=LOG.open("a"), stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(300):      # probing every account + engine start can take ~10 s
        st = read_state()
        if st.get("account"):
            note = "  (all accounts exhausted)" if st.get("all_exhausted") else ""
            print(f"codex-switchboard: {st['account']}{note}", file=sys.stderr)
            return 0
        time.sleep(0.1)
    print(f"codex-switchboard: controller did not report an account; see {LOG}", file=sys.stderr)
    return 1


def cmd_status() -> int:
    st = read_state()
    pid = pid_alive(PID)
    print(f"controller: {'running pid ' + str(pid) if pid else 'stopped'}")
    print(f"engine:     {'reachable pid ' + str(engine_pid()) if engine_reachable() else 'down'}  ({SOCK})")
    if st:
        lim = st.get("limits") or {}
        since = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.get("since", 0)))
        resets = lim.get("resets_at")
        resets = time.strftime("%m-%d %H:%M", time.localtime(resets)) if resets else "?"
        print(f"account:    {st.get('account')}  since {since}  used {lim.get('used')}%  "
              f"allowed {lim.get('allowed')}  reached {lim.get('reached')}  resets {resets}")
        print(f"order:      {' → '.join(ORDER)}")
    if LOG.exists():
        print("recent:")
        for line in LOG.read_text().splitlines()[-8:]:
            print("  " + line[:160])
    return 0


def cmd_switch(name: str) -> int:
    if name not in ORDER:
        print(f"unknown account {name!r}; pool: {', '.join(ORDER)}", file=sys.stderr)
        return 1
    if not pid_alive(PID):
        print("controller is not running; run `switchboard.py ensure` first", file=sys.stderr)
        return 1
    SWITCH_REQUEST.write_text(name)
    for _ in range(100):
        time.sleep(0.2)
        if not SWITCH_REQUEST.exists():
            print(f"switched to {read_state().get('account')}", file=sys.stderr)
            return 0
    print("controller did not pick up the request", file=sys.stderr)
    return 1


def cmd_pick() -> int:
    """For jobs that run `codex exec` themselves: the best account's CODEX_HOME, on stdout."""
    name, p = pick()
    if name is None:
        print("no account can run right now", file=sys.stderr)
        return 1
    print(profile_home(name))
    print(f"{name}: {remaining(p)}% left", file=sys.stderr)
    return 0


def cmd_stop() -> int:
    for label, pid in (("controller", pid_alive(PID)), ("engine", engine_pid())):
        if pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except ProcessLookupError:
                os.kill(pid, signal.SIGTERM)
            print(f"stopped {label} (pid {pid})")
    PID.unlink(missing_ok=True)
    STATE.unlink(missing_ok=True)
    log("stopped")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "run":
        POOL.mkdir(parents=True, exist_ok=True)
        try:
            asyncio.run(Controller().run())
        except KeyboardInterrupt:
            pass
        finally:
            log("controller_exit", pid=os.getpid())
        return 0
    if cmd == "ensure":
        return cmd_ensure()
    if cmd == "status":
        return cmd_status()
    if cmd == "switch" and len(argv) > 2:
        return cmd_switch(argv[2])
    if cmd == "stop":
        return cmd_stop()
    if cmd == "pick":
        POOL.mkdir(parents=True, exist_ok=True)
        return cmd_pick()
    print(__doc__.split("Commands:")[1].split("Logs:")[0], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
