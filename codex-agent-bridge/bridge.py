# # SPDX-License-Identifier: GPL-3.0-or-later
# bridge.py
# ──────────────────────────────────────────────────────────────────────────
#  Codex Free Agent Bridge (adapted from ZeroScript's resilient bridge)
#  Local WebSocket <-> Codex Free MCP server.
#  The browser extension talks to this over ws://127.0.0.1:<PORT>.
#
#  What this bridge exposes to Kimi (aggregated into one tools/list):
#    - Every MCP server declared in config.json (by default: roblox), each
#      spawned as a stdio child and routed by tool name.
#
#  Design goals (robustness first):
#   - Each MCP stdio process is read by ONE dedicated thread; responses are
#     matched by JSON-RPC id (no "read the next line and hope" races).
#   - stderr is drained so a child never blocks on a full pipe.
#   - A dead server is auto-restarted and the failing call retried once.
#   - Tool calls are locked PER SERVER, so a slow server never blocks another.
#   - Every call ALWAYS produces a reply: a result OR a structured error.
#     Nothing ever hangs the agentic loop silently.
# ──────────────────────────────────────────────────────────────────────────
import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time

try:
    # Sibling script (same folder as bridge.py, which Python puts on sys.path
    # automatically) - reused here purely to detect a Studio version bump
    # (see _current_studio_exe below), not to launch anything.
    import launch_studio_mcp as _studio_scan
except Exception:
    _studio_scan = None

try:
    import websockets
except ImportError:
    print("[bridge] Missing dependency. Run:  pip install websockets")
    sys.exit(1)

# Windows consoles often default to a legacy codepage (cp1252): printing
# non-ASCII text then raises UnicodeEncodeError INSIDE the WS handler, which
# kills the connection. Force UTF-8 (best effort). We also keep all console
# output strictly ASCII (no arrows / dots) so nothing garbles on a console that
# stayed on a legacy codepage anyway.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _enable_ansi_colors():
    """On Windows, turn on ANSI escape processing so color codes render instead
    of printing as literal gibberish like "<ESC>[92m". Returns True on success."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        return bool(k.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


HOST = "127.0.0.1"
# Keep in sync with zeroscript-extension/manifest.json "version" - printed at
# startup so a user's terminal output alone tells us which build they're on.
BRIDGE_VERSION = "1.5.5"
PORT = int(os.environ.get("ZS_BRIDGE_PORT", "17613"))
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

# The primary server. It is always present, added by the installer, and can
# never be edited/removed through the extension (it is what ZeroScript is FOR).
PRIMARY_SERVER_ID = "codex"

if _enable_ansi_colors():
    C = {
        "reset": "\033[0m", "dim": "\033[2m", "gr": "\033[92m",
        "yl": "\033[93m", "rd": "\033[91m", "cy": "\033[96m",
        # Bold white-on-red: for a non-technical user, an "ACTION NEEDED" step
        # must look nothing like the routine cyan/yellow status noise around
        # it, or it gets scrolled past unread (seen live 2026-07-13 - the
        # toggle instruction and the boot banner's own yellow re-explanation
        # of the SAME step were visually indistinguishable). Bright-yellow-bg
        # with black text was tried first but reads as low-contrast/washed
        # out on several real terminal color schemes (also seen live) - white
        # on red is the universal high-contrast "act now" pairing.
        "act": "\033[1m\033[97m\033[41m",
    }
else:
    C = {k: "" for k in ("reset", "dim", "gr", "yl", "rd", "cy", "act")}

# Every run appends here (never truncated), so a whole test session - across
# multiple restarts - stays in one file the user can just send us. Each
# process start writes a banner (see main()) so restarts are easy to spot.
LOGS_DIR = os.path.join(HERE, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOGS_DIR, "bridge_debug.log")
try:
    _log_file = open(LOG_PATH, "a", encoding="utf-8", errors="replace")
except Exception:
    _log_file = None


class _Spinner:
    """Terminal-only progress indicator for waits that can run several seconds
    (server launch/handshake, Studio attach grace period) so the console never
    just sits there looking dead - the #1 thing that makes a user assume the
    bridge hung and close the window. Purely cosmetic: writes over its own line
    with \\r, never touches bridge_debug.log, and is skipped entirely when
    stdout isn't a real console (redirected to a file, no ANSI)."""
    FRAMES = "|/-\\"
    # Only ONE spinner may animate at a time: server launches now run in
    # PARALLEL (see MCPManager.start_all), and several spinners fighting over
    # the same console line with \r produced interleaved garbage. Whoever
    # acquires this lock animates; the others silently skip (the log lines
    # around them still tell the story).
    _active = threading.Lock()

    def __init__(self, label):
        self.label = label
        self._stop = threading.Event()
        self._thread = None
        self._owns_lock = False

    def __enter__(self):
        if sys.stdout.isatty() and _Spinner._active.acquire(blocking=False):
            self._owns_lock = True
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            # Wipe the spinner line so the next log() line doesn't get glued
            # onto trailing spinner characters.
            print("\r" + " " * (len(self.label) + 4) + "\r", end="", flush=True)
        if self._owns_lock:
            _Spinner._active.release()

    def _run(self):
        i = 0
        while not self._stop.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            print(f"\r{C['dim']}{self.label} {frame}{C['reset']}", end="", flush=True)
            i += 1
            self._stop.wait(0.15)


def _clear_spinner_line():
    """Wipe whatever a live _Spinner (running on its own thread, mid-frame) left
    on the current console line via bare \\r writes, so the next print() below
    doesn't get glued onto its trailing characters - seen live 2026-07-14: an
    action_banner() fired while '[roblox] starting... -' was still mid-line and
    the red box rendered smashed onto it instead of starting on a fresh line.
    \\033[K (clear to end of line) doesn't depend on knowing the spinner's label
    length the way Spinner.__exit__'s own wipe does."""
    if sys.stdout.isatty():
        print("\r\033[K", end="", flush=True)


def log(msg, color="dim", terminal=True):
    """terminal=False: written to bridge_debug.log only, not the console. Use
    for noisy/technical detail (raw stderr from child MCP servers, per-call
    traces) that would bury the handful of lines a non-technical user actually
    needs to read. Nothing is ever lost - it all still lands in the file."""
    if terminal:
        _clear_spinner_line()
        ts = time.strftime("%H:%M:%S")
        print(f"{C['dim']}{ts}{C['reset']} {C.get(color,'')}{msg}{C['reset']}", flush=True)
    if _log_file:
        try:
            _log_file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
            _log_file.flush()
        except Exception:
            pass


def action_banner(lines):
    """Print a step the USER must physically go do, styled so it cannot be
    mistaken for routine status/warning noise (see the 'act' color above).
    Framed with blank lines so it visually stands alone in a scrolling
    terminal - a non-technical user should be able to glance at the window
    and immediately spot this without reading everything above it.

    Every line (header, content, footer) is padded to the SAME width so the
    yellow block renders as one clean rectangle - an earlier version padded
    each line to a fixed guess independently, which produced a ragged block
    with mismatched edges on a real console (seen live 2026-07-13)."""
    header = "ACTION NEEDED"
    width = max([len(header) + 8] + [len(ln) for ln in lines]) + 2
    top = f">>> {header} " + ">" * max(0, width - len(header) - 5)
    _clear_spinner_line()
    print()
    print(f"{C['act']}  {top.ljust(width)}{C['reset']}")
    for ln in lines:
        print(f"{C['act']}  {ln.ljust(width)}{C['reset']}")
    print(f"{C['act']}  {'>' * width}{C['reset']}")
    print()
    if _log_file:
        try:
            _log_file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} ACTION NEEDED: "
                             f"{' | '.join(lines)}\n")
            _log_file.flush()
        except Exception:
            pass


# Roblox Studio exposes its built-in MCP server on this loopback port. StudioMCP
# (and our bridge, via it) reaches Studio through it.
STUDIO_MCP_PORT = 13469


def _port_owner(port):
    """(pid, name, path) of the process LISTENING on `port`, or None. Win32 only."""
    if sys.platform != "win32":
        return None
    # BOTH stacks: "-p TCP" alone is IPv4-only, and a squatter listening on
    # [::1]:<port> (IPv6 loopback) was then completely invisible to this probe
    # even while Get-NetTCPConnection showed it plainly (the likely reason the
    # boot-time squatter check stayed silent on a machine where ropilot
    # provably held the port - see the 2026-07-13 live report).
    out = ""
    for proto in ("TCP", "TCPv6"):
        try:
            out += subprocess.run(
                ["netstat", "-ano", "-p", proto],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=8,
            ).stdout
        except Exception:
            pass
    if not out:
        return None
    pid = None
    # v4 lines end the local address in ":<port>", v6 in "]:<port>" - matching
    # on the ":<port> " suffix (with the column gap) covers both shapes.
    needle = f":{port} "
    for line in out.splitlines():
        if "LISTENING" in line and needle in line:
            parts = line.split()
            if parts and parts[-1].isdigit():
                pid = parts[-1]
                break
    if not pid:
        return None
    name, path = "?", ""
    try:
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"$p=Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
             f"if($p){{$p.Name; $p.Path}}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8,
        ).stdout.splitlines()
        ps = [l.strip() for l in ps if l.strip()]
        if ps:
            name = ps[0]
            path = ps[1] if len(ps) > 1 else ""
    except Exception:
        pass
    return (pid, name, path)


def _roblox_studio_app_running():
    """True/False whether a real Roblox Studio window process exists, or None
    if this can't be determined (non-Windows, or the check itself failed)."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq RobloxStudioBeta.exe"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8,
        ).stdout
    except Exception:
        return None
    return "RobloxStudioBeta.exe" in out


def _kill_orphan_studio_mcp():
    """Kill leftover StudioMCP.exe processes from a PREVIOUS session/crash.

    StudioMCP.exe is Roblox's own MCP proxy; launch_studio_mcp.py spawns one
    as a direct child every time the bridge starts. If an earlier restart's
    tree-kill missed the grandchild (a reparenting race), or Studio itself
    crashed and left its own StudioMCP.exe running (seen live 2026-07-11:
    RobloxStudioBeta.exe zombied after two RobloxCrashHandler.exe events),
    the orphan keeps LISTENING on Studio's MCP port. Every StudioMCP.exe we
    launch afterward - even a freshly restarted one - just connects to that
    zombie instead of a real Studio, so the bridge reports "Studio connected"
    forever even with Studio fully closed. studio_watch's auto-restart cannot
    fix this on its own: restarting our proxy still lands on the same zombie.

    Only acts when NO real Studio app is running at all - in that state any
    existing StudioMCP.exe is unambiguously orphaned (a legitimate one only
    exists to serve a live Studio), so it is safe to auto-kill without asking.
    If Studio IS running (or this can't be determined), this is a no-op: a
    live StudioMCP.exe might be legitimately serving it, so nothing is
    touched - this must never risk killing a working connection.
    """
    if sys.platform != "win32":
        return
    if _roblox_studio_app_running() is not False:
        return
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq StudioMCP.exe"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8,
        ).stdout
    except Exception:
        return
    if "StudioMCP.exe" not in out:
        return
    log("Found leftover StudioMCP.exe process(es) with no Roblox Studio running - "
        "cleaning them up (known cause of a phantom 'Studio connected' state).", "yl")
    try:
        subprocess.run(["taskkill", "/F", "/IM", "StudioMCP.exe"],
                       capture_output=True, text=True, timeout=8)
    except Exception as e:
        log(f"could not clean up orphaned StudioMCP.exe: {e}", "rd")


def _descendant_pids(root_pid):
    """Set of PIDs = root_pid + every descendant, or None if the process tree
    could not be read (in which case callers must NOT make kill decisions)."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | ForEach-Object "
             "{ \"$($_.ProcessId) $($_.ParentProcessId)\" }"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10,
        ).stdout
    except Exception:
        return None
    children = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    if not children:
        return None
    pids = {int(root_pid)}
    stack = [int(root_pid)]
    while stack:
        for c in children.get(stack.pop(), []):
            if c not in pids:
                pids.add(c)
                stack.append(c)
    return pids


def _reclaim_studio_port(client):
    """Kill a StudioMCP.exe that owns Studio's MCP port but is NOT our own child.

    The deadlock this breaks (reported live, survives every restart combo):
    a zombie StudioMCP.exe from a crashed session keeps LISTENING on 13469.
    The user reopens Studio -> its MCP plugin does its ONE-SHOT registration
    against the ZOMBIE (wasted). The user restarts the bridge -> Studio is now
    running, so _kill_orphan_studio_mcp's safety guard skips the cleanup, and
    check_studio_port waves the zombie through too (its path IS under Roblox).
    Our fresh StudioMCP can't own the port, Studio never re-registers on its
    own -> 0 tools forever, no restart order can fix it by hand.

    Ownership is decided by PID, not heuristics: we know the PID of the
    launcher we spawned (client.proc), so a StudioMCP.exe holding the port
    outside that process tree is a leftover by definition - Studio open or
    not. If the process tree can't be read, we do nothing (never risk killing
    our own healthy child on bad data). Returns True if a zombie was killed;
    the caller must then restart the roblox proxy (safe here even with Studio
    open: the plugin's single registration already went to the zombie, so
    there is no attempt left for a restart to collide with) AND tell the user
    to open Assistant Settings > MCP Servers so the plugin re-registers.
    """
    owner = _port_owner(STUDIO_MCP_PORT)
    if not owner:
        return False
    pid, name, path = owner
    # Only ever kill a StudioMCP.exe. Studio itself holding the port is fine;
    # a non-Roblox squatter is check_studio_port's (interactive) job.
    if "studiomcp" not in (name or "").lower():
        return False
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return False
    if client is not None and client.proc is not None and client.is_alive():
        tree = _descendant_pids(client.proc.pid)
        if tree is None or pid_i in tree:
            return False  # ours, or unknowable - leave it alone
    log(f"port {STUDIO_MCP_PORT} is held by a StudioMCP.exe (pid {pid_i}) that this "
        "bridge did NOT launch - a leftover from a previous session. Studio "
        "registered to it, so our proxy sees 0 tools.", "yl")
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid_i)],
                       capture_output=True, text=True, timeout=8)
    except Exception as e:
        log(f"could not kill the leftover StudioMCP.exe: {e}", "rd")
        return False
    log(f"killed the leftover StudioMCP.exe (pid {pid_i}) to free Studio's MCP port.", "cy")
    return True


def _process_cmdline(pid):
    """Full command line of `pid`, or "" if it can't be read. Win32 only.

    Used to tell OUR OWN kind of process (a python running bridge.py) apart
    from an unrelated app that merely happens to listen on the same port -
    the process NAME is just "python"/"py"/"pythonw", far too generic to kill
    on. The command line is what proves it is a leftover bridge."""
    if sys.platform != "win32":
        return ""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\" "
             f"-ErrorAction SilentlyContinue).CommandLine"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8,
        ).stdout
    except Exception:
        return ""
    return (out or "").strip()


def _reclaim_bridge_port():
    """Free OUR OWN listen port (17613) from a leftover bridge before we bind.

    The common failure (reported live, WinError 10048 on bind): the user
    relaunches start.bat while an earlier bridge.py is still running - window
    closed with the X instead of Ctrl+C, a previous crash that left a detached
    python, or a double double-click. The old process still holds the port, so
    websockets.serve() dies on bind with a cryptic (localised) OSError and the
    whole bridge exits code 1.

    We reuse _port_owner (already generic over the port) and only ever kill a
    process we can PROVE is another bridge.py - never a same-name innocent
    (some unrelated python listening on 17613): the guard is the command line
    containing "bridge.py", plus an explicit self-exclusion by PID. Anything
    else (a non-python app, or a python whose cmdline we can't read) is left
    alone and surfaced to the user by the caller's friendly bind-error path.
    Returns True if a leftover bridge was killed."""
    owner = _port_owner(PORT)
    if not owner:
        return False
    pid, name, path = owner
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_i == os.getpid():
        return False  # never kill ourselves (defensive; we haven't bound yet)
    # Must look like a python interpreter AND be running bridge.py. Killing on
    # the port alone would murder whatever legitimately owns 17613.
    if "python" not in (name or "").lower() and "py" != (name or "").lower():
        return False
    cmdline = _process_cmdline(pid_i)
    if "bridge.py" not in cmdline.lower():
        log(f"port {PORT} is held by pid {pid_i} ('{name}') but it does not look "
            f"like a ZeroScript bridge - leaving it alone.", "yl")
        return False
    log(f"port {PORT} is held by a leftover ZeroScript bridge (pid {pid_i}) from a "
        "previous session - killing it so this one can start.", "yl")
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid_i)],
                       capture_output=True, text=True, timeout=8)
    except Exception as e:
        log(f"could not kill the leftover bridge (pid {pid_i}): {e}", "rd")
        return False
    log(f"killed the leftover bridge (pid {pid_i}); the port is free now.", "cy")
    return True


def _kill_port_squatter():
    """Kill a NON-Roblox process holding Studio's MCP port, no questions asked.

    Called only when the child's stderr has PROVEN the port is hijacked (see
    MCPClient.saw_foreign_ws_host - StudioMCP connected to a foreign host and
    could not parse its protocol; the ropilot case). At that point there is no
    ambiguity left to justify check_studio_port's interactive prompt, and the
    prompt was itself a trap: many users never answer it, and the one-shot boot
    check often runs a beat before a background helper (ropilot) grabs the
    port. Here we have hard evidence, so kill the squatter outright. Returns
    (killed, name) so the caller can tell the user which app to uninstall /
    remove from startup, since it will otherwise reclaim the port on next boot.
    """
    owner = _port_owner(STUDIO_MCP_PORT)
    if owner:
        pid, name, path = owner
        if "roblox" in (path or "").lower() or "studiomcp" in (name or "").lower():
            return False, None  # legitimate Studio-side owner; not a squatter
        log(f"port {STUDIO_MCP_PORT} is hijacked by '{name}' (pid {pid}, {path}).", "yl")
        log("    StudioMCP connected to it instead of Roblox Studio - that is why "
            "there are 0 tools.", "yl")
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True, timeout=8)
        except Exception as e:
            log(f"could not kill '{name}': {e}", "rd")
            return False, name
        log(f"killed '{name}' so Studio can use the port.", "cy")
        return True, name
    # We could NOT resolve who owns the port, yet StudioMCP's stderr proved the
    # port is hijacked (this function is only called under that proof). This is
    # the state that used to fail SILENTLY: _port_owner returning None (e.g. a
    # squatter listening on IPv6 loopback that an IPv4-only netstat missed, or
    # any netstat quirk) left the user staring at 0 tools with no explanation.
    # Never be silent here. Try a name-based fallback for the known offender
    # (ropilot ships a background helper that squats this port), then always
    # tell the user what we know.
    log(f"port {STUDIO_MCP_PORT} is hijacked (StudioMCP could not talk to Roblox "
        "Studio on it) but the owning process could not be identified by port.", "yl")
    # ropilot is a multi-process app (validated live 2026-07-13): the port is
    # held by ropilot-infra-helper.exe, supervised by ropilot-infra.exe. Kill
    # both so the supervisor can't just respawn the helper and re-grab the port.
    killed_name = None
    for img in ("ropilot-infra-helper.exe", "ropilot-infra.exe", "ropilot.exe"):
        try:
            res = subprocess.run(["taskkill", "/F", "/IM", img],
                                 capture_output=True, text=True, timeout=8)
        except Exception:
            continue
        if res.returncode == 0:
            killed_name = img
            log(f"killed '{img}' (known port squatter) so Studio can use the port.", "cy")
    if killed_name:
        return True, killed_name
    log("    Could not auto-kill it. Find it manually: run  netstat -ano | "
        f"findstr {STUDIO_MCP_PORT}  then end that PID in Task Manager.", "yl")
    return False, None


def _print_squatter_hint(name):
    """After killing a port squatter (e.g. ropilot), tell the user how to stop
    it coming back - it is a background helper that respawns on the next boot
    and re-grabs the port before Studio, which is why a PC reboot never fixed
    this class of 0-tools report."""
    app = name or "the other app"
    action_banner([
        f"'{app}' fights Roblox Studio for its connection - it will keep",
        "coming back after every restart until you remove it.",
        f"1. Uninstall '{app}' (or remove it from Windows startup).",
        "2. In Roblox Studio: Assistant Settings > MCP Servers,",
        "   turn OFF then back ON 'Enable Studio as MCP server'.",
    ])


def _print_reregister_hint():
    """The one user action that completes a zombie-kill recovery: Studio's MCP
    plugin registers only once per boot and that attempt went to the zombie,
    so after the kill + proxy restart the user must make it register again."""
    # Opening the panel alone is technically enough to re-register, but we tell
    # the user to toggle OFF/ON to be sure - a toggle strictly implies opening
    # the panel, so it can never do less, and it removes any ambiguity about
    # whether "just looking at it" counted. Same wording as the squatter/no-place
    # banners so all three read as one identical instruction, not three variants.
    action_banner([
        "Go to Roblox Studio now.",
        "Turn OFF then back ON: Assistant Settings > MCP Servers",
        "         > 'Enable Studio as MCP server'",
        "Wait about 10 seconds - this window will turn green.",
    ])


def check_studio_port():
    """Warn (and optionally kill) a NON-Roblox process squatting Studio's MCP port.

    A third-party tool (e.g. "ropilot") that binds 13469 before Studio does
    hijacks the MCP channel: StudioMCP connects to IT instead of Studio, the
    handshake succeeds but tools/list never answers -> the bridge sees 0 tools.
    This is silent and brutal to diagnose, so we surface it up front.
    """
    owner = _port_owner(STUDIO_MCP_PORT)
    if not owner:
        return False
    pid, name, path = owner
    # The legitimate holder is Studio itself / a Roblox helper: its path lives
    # under a "...\Roblox\..." folder. Anything else is an intruder.
    if "roblox" in (path or "").lower():
        return False
    where = path or name
    log(f"port {STUDIO_MCP_PORT} (Studio's MCP port) is held by a non-Roblox process:", "yl")
    log(f"    {name} (pid {pid})  {where}", "yl")
    log("    This will block Studio's tools (the bridge will see 0 tools).", "yl")
    try:
        ans = input("    Kill this process so Studio can use the port? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = ""
    if ans in ("y", "yes", "o", "oui"):
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True, timeout=8)
            log(f"killed {name} (pid {pid}). Studio can use the port now.", "cy")
            # Tell the user the finishing step IMMEDIATELY, here, instead of only
            # after the ~48s server-launch grace loop that follows: killing the
            # squatter frees the port, but Studio's MCP plugin registers only
            # once per boot and that attempt already went to the squatter, so it
            # will NOT re-attach on its own - a toggle is needed. Printing this
            # now (not 48s later, after start_all's grace loop) is what turns a
            # ~1-minute "why is nothing happening" wait into an act-right-away
            # instruction. Uses action_banner (not log) so a non-technical user
            # visually cannot miss it among the surrounding status lines - seen
            # live indistinguishable when both used the same plain color.
            action_banner([
                "Go to Roblox Studio now.",
                "Turn OFF then back ON: Assistant Settings > MCP Servers",
                "         > 'Enable Studio as MCP server'",
                "Wait about 10 seconds - this window will turn green.",
            ])
            return True  # a squatter WAS killed -> Studio must reclaim the port
        except Exception as e:
            log(f"could not kill it: {e}", "rd")
    else:
        log("left it running. Close it yourself, then restart the bridge.", "yl")
    return False


_TRANSIENT_STUDIO_MARKERS = (
    "no roblox studio instance", "no active studio", "studio instance is connect",
    "studio instance connected", "not connected to", "no studio instance",
)


def _looks_like_transient_studio_drop(text):
    low = (text or "").lower()
    return any(m in low for m in _TRANSIENT_STUDIO_MARKERS)


# ── config.json read / write (for extension-driven add/remove) ──────────────
def _read_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                cfg.setdefault("mcpServers", {})
                return cfg
        except Exception as e:
            log(f"config.json unreadable ({e}) - starting from a fresh one", "yl")
    return {
        "mcpServers": {
            PRIMARY_SERVER_ID: {
                "name": "Monolith MCP",
                "kind": "monolith",
                "command": r"D:\UE5_editor\UE_5.8\ActionRoguelike-master\ActionRoguelike-master\Plugins\Monolith\Binaries\monolith_proxy.exe",
                "args": [],
                "enabled": False,
                "toolkit": "monolith",
                "env": {
                    "MONOLITH_URL": "http://localhost:9316/mcp",
                    "MONOLITH_HTTP_TIMEOUT": "120",
                },
            }
        }
    }


def _write_config(cfg):
    """Atomic write so a crash mid-write never leaves a truncated config.json."""
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def config_add_server(server_id, command, args=None, env=None):
    """Add/replace an addon server in config.json. Refuses to touch the primary
    (roblox) server. Returns (ok, error)."""
    sid = (server_id or "").strip()
    if not sid:
        return False, "server id is required"
    if sid == PRIMARY_SERVER_ID:
        return False, f"'{PRIMARY_SERVER_ID}' is the primary server and cannot be edited"
    if not (command or "").strip():
        return False, "a command is required"
    cfg = _read_config()
    spec = {"command": command.strip(), "args": list(args or []), "enabled": True}
    if env:
        spec["env"] = dict(env)
    cfg["mcpServers"][sid] = spec
    try:
        _write_config(cfg)
    except Exception as e:
        return False, f"could not write config.json: {e}"
    return True, None


def config_set_server_enabled(server_id, enabled):
    """Enable or disable a configured MCP server without removing its config."""
    sid = (server_id or "").strip()
    if not sid:
        return False, "server id is required"
    cfg = _read_config()
    spec = cfg.get("mcpServers", {}).get(sid)
    if not isinstance(spec, dict):
        return False, f"server '{sid}' is not in the config"
    spec["enabled"] = bool(enabled)
    try:
        _write_config(cfg)
    except Exception as e:
        return False, f"could not write config.json: {e}"
    return True, None


def config_set_toolkit_skill(server_id, skill_id, enabled):
    """Persist a per-server toolkit skill override without restarting MCP."""
    sid = (server_id or "").strip()
    skill = (skill_id or "").strip()
    if not sid or not skill:
        return False, "server id and skill id are required"
    cfg = _read_config()
    spec = cfg.get("mcpServers", {}).get(sid)
    if not isinstance(spec, dict):
        return False, f"unknown MCP server: {sid}"
    overrides = spec.setdefault("toolkitSkillOverrides", {})
    if not isinstance(overrides, dict):
        overrides = {}
        spec["toolkitSkillOverrides"] = overrides
    overrides[skill] = bool(enabled)
    try:
        _write_config(cfg)
    except Exception as e:
        return False, f"could not write config.json: {e}"
    return True, None


def _hydrate_toolkit_skills(toolkit):
    """Load markdown skills declared by a toolkit profile for the UI/prompt."""
    if not isinstance(toolkit, dict):
        return toolkit
    skills = list(toolkit.get("skills") or [])
    roots = []
    if toolkit.get("skillRoot"):
        roots.append({"path": toolkit["skillRoot"], "prefix": ""})
    for item in toolkit.get("skillRoots", []) or []:
        if isinstance(item, str):
            roots.append({"path": item, "prefix": "community"})
        elif isinstance(item, dict) and item.get("path"):
            roots.append(item)
    if not roots:
        return toolkit
    default_enabled = bool(toolkit.get("defaultSkillEnabled", False))
    for root_spec in roots:
        root = os.path.abspath(os.path.expandvars(str(root_spec.get("path", ""))))
        if not os.path.isdir(root):
            continue
        prefix = str(root_spec.get("prefix", "")).strip()
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for entry in entries:
            folder = os.path.join(root, entry)
            if not os.path.isdir(folder):
                continue
            docs = sorted(name for name in os.listdir(folder) if name.lower().endswith(".md"))
            if not docs:
                continue
            path = os.path.join(folder, docs[0])
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                continue
            skill_id = f"{prefix}:{entry}" if prefix else entry
            title = entry.replace("-", " ").replace("_", " ").strip().title()
            label = f"{prefix.title()} / {title}" if prefix else title
            skill = {
                "id": skill_id,
                "name": label,
                "enabled": default_enabled,
                "source": path,
                "instructions": content,
            }
            if not any(isinstance(existing, dict) and existing.get("id") == skill_id for existing in skills):
                skills.append(skill)
    out = dict(toolkit)
    out["skills"] = skills
    return out


def config_remove_server(server_id):
    """Remove an addon server from config.json. Refuses the primary server."""
    sid = (server_id or "").strip()
    if sid == PRIMARY_SERVER_ID:
        return False, f"'{PRIMARY_SERVER_ID}' is the primary server and cannot be removed"
    cfg = _read_config()
    if sid not in cfg.get("mcpServers", {}):
        return False, f"server '{sid}' is not in the config"
    del cfg["mcpServers"][sid]
    try:
        _write_config(cfg)
    except Exception as e:
        return False, f"could not write config.json: {e}"
    return True, None


def restart_self():
    """Replace this process with a fresh one so config.json is reloaded from
    scratch. Children are killed first to free their stdio pipes / ports before
    the new instance claims them. Never returns on success (os.execv)."""
    log("restarting bridge to load new server config...", "yl")
    try:
        for c in mgr.clients.values():
            c.stop()
    except Exception:
        pass
    if _log_file:
        try:
            _log_file.flush()
        except Exception:
            pass
    # Spawn with an explicit script path and then exit. os.execv combined with
    # the py launcher can preserve a directory-valued argv[0] on Windows,
    # producing "can't find '__main__' module ..." after a config restart.
    script = os.path.abspath(__file__)
    try:
        subprocess.Popen([sys.executable, script], cwd=HERE, close_fds=True)
    except Exception as e:
        log(f"could not restart bridge: {e} - please restart it manually", "rd")
    os._exit(0)


# ══════════════════════════════════════════════════════════════════════════
#  HARDENED MCP CLIENT  (one per server in config.json)
# ══════════════════════════════════════════════════════════════════════════
class MCPClient:
    def __init__(self, server_id, command, args, env=None, enabled=True, name=None, kind=None, toolkit=None):
        self.id = server_id
        self.name = name or server_id
        self.kind = kind or "mcp"
        self.toolkit = toolkit if isinstance(toolkit, dict) else {}
        self.command = command
        self.args = list(args or [])
        self.env = env or {}
        self.enabled = bool(enabled)
        self.proc = None
        self.req_id = 1
        self.write_lock = threading.Lock()
        self.call_lock = threading.Lock()   # serialize tool calls (single stdio pipe)
        self.pending = {}                    # id -> queue.Queue (one slot)
        self.pend_lock = threading.Lock()
        self.tools_cache = []
        # Set by MCP notifications/tools/list_changed.
        # Reader thread chỉ đánh dấu dirty; server_watch sẽ refresh ở thread khác.
        self.tools_dirty = False
        self.start_lock = threading.Lock()
        self._reader_thread = None
        # Crash-loop forensics (read by server_watch). The auto-restart used to
        # hide a server that something else kills over and over: the terminal
        # showed an endless quiet restart cycle with no explanation at all. We
        # keep just enough state to NAME the problem in the terminal instead:
        #  - last_exit: exit code from the final _reader EOF (crash vs kill hint)
        #  - stderr_tail: the last few stderr lines (usually the actual reason -
        #    port bind failure, missing dependency, crash trace)
        #  - restart_times: recent auto-restart timestamps (loop detector input)
        #  - loop_warned_at: throttle so the big red banner prints once per
        #    cooldown, not every 5s poll
        self.last_exit = None
        self.stderr_tail = []
        self.restart_times = []
        self.loop_warned_at = 0.0
        # Set when the configured command itself couldn't be launched at all
        # (e.g. 'uvx' not installed / not on PATH). This is NOT a crash - the
        # process never existed, so last_exit/stderr_tail stay empty and the
        # generic crash-loop banner used to print "the server printed no error
        # output before dying", which is misleading for a config problem the
        # user can fix in seconds. Kept across restarts so the banner can name
        # the real cause instead.
        self.start_error = None
        # Set when StudioMCP's stderr shows it connected to a FOREIGN WS host on
        # Studio's MCP port (not Studio). The unmistakable signature is a parse
        # error on the host's messages ("missing field `type`") - Studio speaks
        # the expected protocol, a squatter like ropilot speaks its own. This is
        # a timing-independent proof that the port is hijacked, unlike the
        # one-shot check_studio_port() boot probe which can miss a squatter that
        # grabs the port a moment after boot (seen live 2026-07-13: ropilot took
        # the port ~1s after the boot check ran, so nothing was flagged).
        self.saw_foreign_ws_host = False

    # ── lifecycle ─────────────────────────────────────────────────────────
    def _resolve(self, s):
        return os.path.expandvars(os.path.expanduser(str(s)))

    def start(self):
        with self.start_lock:
            if not self.enabled:
                return
            if self.is_alive():
                return
            cmd = [self._resolve(self.command)] + [self._resolve(a) for a in self.args]
            # A bare .py command (relative paths resolve against the bridge dir)
            # is run with the SAME interpreter the bridge itself uses, so it works
            # even on installs where only the `py` launcher exists (no `python`
            # on PATH). This is how the Studio MCP launcher is wired by default.
            if cmd[0].lower().endswith(".py"):
                script = cmd[0]
                if not os.path.isabs(script):
                    script = os.path.join(HERE, script)
                cmd = [sys.executable, script] + cmd[1:]
            # On Windows, npx/npm/yarn/pnpm/bunx are .cmd shims that Popen can't
            # launch directly (WinError 2). Run them through cmd.exe so any
            # node-based MCP server "just works" from config.json.
            if sys.platform == "win32":
                base = os.path.basename(cmd[0]).lower()
                if base in ("npx", "npm", "yarn", "pnpm", "bunx"):
                    cmd = ["cmd.exe", "/c"] + cmd
            env = dict(os.environ)
            for k, v in self.env.items():
                env[k] = self._resolve(v)
            log(f"[{self.id}] launching  ({' '.join(cmd)})", "cy")
            with _Spinner(f"    [{self.id}] starting..."):
                try:
                    self.proc = subprocess.Popen(
                        cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                        encoding="utf-8",
                        errors="replace",
                        cwd=HERE,
                        env=env,
                    )
                except FileNotFoundError:
                    # The OS couldn't find cmd[0] at all - this is a config
                    # problem (missing dependency, typo, not on PATH), not a
                    # transient crash. Auto-restart will keep retrying (the
                    # user may install it later), but name the real cause so
                    # it doesn't just look like an endless silent restart loop.
                    self.start_error = (
                        f"command not found: '{cmd[0]}' - is it installed and on PATH? "
                        f"(configured for server '{self.id}' in config.json)"
                    )
                    log(f"[{self.id}] {self.start_error}", "rd")
                    raise
                except OSError as e:
                    self.start_error = f"could not launch '{cmd[0]}': {e}"
                    log(f"[{self.id}] {self.start_error}", "rd")
                    raise
                else:
                    self.start_error = None
                with self.pend_lock:
                    self.pending.clear()
                self.saw_foreign_ws_host = False  # fresh process, fresh verdict
                self._reader_thread = threading.Thread(target=self._reader, args=(self.proc,), daemon=True)
                self._reader_thread.start()
                threading.Thread(target=self._stderr_drain, args=(self.proc,), daemon=True).start()

                # MCP handshake.
                self._request("initialize", {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "zeroscript-bridge", "version": "1.0"},
                }, timeout=30)
                self._notify("notifications/initialized")
                # Some MCP servers (notably Roblox's StudioMCP) advertise 0 tools at
                # the instant initialize returns, because they connect to their
                # backend (the running Studio) a moment AFTER the stdio handshake.
                # A single tools/list then caches an empty list forever. So if we
                # get nothing, retry for a few seconds to let the backend attach.
                # Short per-attempt timeout so the bridge never looks frozen if the
                # server stays silent (e.g. Studio not open yet); ~12s total budget.
                for _ in range(12):
                    if self.refresh_tools(timeout=3):
                        break
                    if not self.is_alive():
                        break
                    time.sleep(1.0)
            log(f"[{self.id}] MCP server up  ({len(self.tools_cache)} tools advertised)", "cy")

    def is_alive(self):
        return self.proc is not None and self.proc.poll() is None

    def restart(self):
        log(f"[{self.id}] restarting...", "yl")
        self.stop()
        time.sleep(0.4)
        self.start()

    def stop(self):
        with self.pend_lock:
            for q in self.pending.values():
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
            self.pending.clear()
        if self.proc:
            # proc.terminate() (TerminateProcess on Windows) only kills THIS
            # pid. Our command is often a wrapper (e.g. launch_studio_mcp.py)
            # that Popen()s a real child (StudioMCP.exe) to own the stdio
            # pipes - terminate() would leave that child orphaned, still bound
            # to Studio's MCP port, fighting the next restart's fresh instance.
            # taskkill /T kills the whole tree.
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                        capture_output=True, timeout=8,
                    )
                else:
                    self.proc.terminate()
            except Exception:
                pass
        self.proc = None

    # ── io threads ────────────────────────────────────────────────────────
    def _reader(self, proc):
        stream = proc.stdout
        while True:
            try:
                line = stream.readline()
            except Exception:
                break
            if line == "":  # EOF -> process exited
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue  # stray non-JSON log on stdout
            mid = msg.get("id")
            if mid is None:
                if msg.get("method") == "notifications/tools/list_changed":
                    self.tools_dirty = True
                    log(
                        f"[{self.id}] tools/list_changed received",
                        "cy",
                        terminal=False,
                    )
                continue
            with self.pend_lock:
                q = self.pending.get(mid)
            if q is not None:
                try:
                    q.put_nowait(msg)
                except Exception:
                    pass
        code = proc.poll()
        self.last_exit = code  # kept for the crash-loop banner in server_watch
        log(f"[{self.id}] stdout closed (process ended, exit code {code})", "rd")
        with self.pend_lock:
            for q in self.pending.values():
                try:
                    q.put_nowait(None)
                except Exception:
                    pass

    def _stderr_drain(self, proc):
        # Surface the child's stderr instead of silently discarding it - this
        # is often the ONLY clue why a server died (crash trace, port bind
        # failure, missing Studio, etc).
        try:
            for line in iter(proc.stderr.readline, ""):
                line = line.rstrip()
                if line:
                    # Ring buffer of the last stderr lines: when the server
                    # enters a crash loop, these are printed in the terminal
                    # banner - they are usually the only real explanation
                    # (port already in use, module not found, crash trace).
                    self.stderr_tail.append(line)
                    if len(self.stderr_tail) > 8:
                        self.stderr_tail.pop(0)
                    # Squatter signature: StudioMCP connected to a non-Studio host
                    # on the MCP port and can't parse its protocol. This is the
                    # ropilot hijack, timing-independent (see saw_foreign_ws_host).
                    low = line.lower()
                    if "failed to parse message from ws host" in low or "missing field `type`" in low:
                        self.saw_foreign_ws_host = True
                    log(f"[{self.id}] stderr: {line}", "yl", terminal=False)
        except Exception:
            pass

    # ── jsonrpc ───────────────────────────────────────────────────────────
    def _next_id(self):
        with self.write_lock:
            rid = self.req_id
            self.req_id += 1
            return rid

    def _notify(self, method, params=None):
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        with self.write_lock:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()

    def _request(self, method, params, timeout):
        if not self.is_alive():
            raise RuntimeError(f"server '{self.id}' is not running")
        rid = self._next_id()
        q = queue.Queue(maxsize=1)
        with self.pend_lock:
            self.pending[rid] = q
        try:
            payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
            with self.write_lock:
                self.proc.stdin.write(json.dumps(payload) + "\n")
                self.proc.stdin.flush()
            try:
                return q.get(timeout=timeout)
            except queue.Empty:
                return None
        finally:
            with self.pend_lock:
                self.pending.pop(rid, None)

    # ── high-level ────────────────────────────────────────────────────────
    def refresh_tools(self, timeout=20):
        msg = self._request("tools/list", {}, timeout=timeout)
        if msg and "result" in msg:
            self.tools_cache = msg["result"].get("tools", [])
            self.tools_dirty = False
        return self.tools_cache

    def call_tool(self, name, arguments, timeout):
        """Returns {"text":..., "images":[...], "is_error":bool}."""
        with self.call_lock:
            if not self.is_alive():
                self.restart()

            msg = self._request(
                "tools/call",
                {"name": name, "arguments": arguments},
                timeout,
            )

            if msg is None:
                if not self.is_alive():
                    self.restart()
                    msg = self._request(
                        "tools/call",
                        {"name": name, "arguments": arguments},
                        timeout,
                    )

                if msg is None:
                    raise TimeoutError(
                        f"No response from server '{self.id}' after {timeout}s."
                    )

            if msg.get("error"):
                err = msg["error"]
                raise RuntimeError(
                    err.get("message", json.dumps(err))
                )

            result = msg.get("result", {}) or {}
            content = result.get("content", []) or []

            text = "\n".join(
                item.get("text", "")
                for item in content
                if item.get("type") == "text"
            )

            images = [
                {
                    "data": item["data"],
                    "mimeType": item.get(
                        "mimeType",
                        "image/jpeg",
                    ),
                }
                for item in content
                if item.get("type") == "image"
                and item.get("data")
            ]

            if not text and not images and content:
                text = json.dumps(content)[:4000]

            return {
                "text": text,
                "images": images,
                "is_error": bool(
                    result.get("isError", False)
                ),
            }



# ══════════════════════════════════════════════════════════════════════════
#  MANAGER  - aggregates every MCP server, routes by tool name.
# ══════════════════════════════════════════════════════════════════════════
class MCPManager:
    def __init__(self):
        self.clients = {}          # server_id -> MCPClient
        self.index = {}            # advertised_name -> (holder, real_name)
        self.index_lock = threading.Lock()

    def load_config(self):
        cfg = _read_config()
        servers = cfg.get("mcpServers", {}) or {}
        profiles = cfg.get("mcpToolkits", {}) or {}
        for sid, spec in servers.items():
            kind = spec.get("kind") or sid
            toolkit = spec.get("toolkit")
            if isinstance(toolkit, str):
                toolkit = profiles.get(toolkit)
            if not isinstance(toolkit, dict):
                toolkit = profiles.get(kind) or profiles.get(sid) or profiles.get("default") or {}
            else:
                toolkit = dict(toolkit)
            toolkit = _hydrate_toolkit_skills(toolkit)
            # A toolkit profile is reusable; skill switches belong to the
            # individual MCP server so changing Monolith does not change any
            # other server that happens to use the same profile.
            overrides = spec.get("toolkitSkillOverrides") or {}
            if isinstance(overrides, dict) and isinstance(toolkit.get("skills"), list):
                toolkit["skills"] = [
                    dict(skill, enabled=bool(overrides[skill.get("id")]))
                    if isinstance(skill, dict) and skill.get("id") in overrides else skill
                    for skill in toolkit["skills"]
                ]
            self.clients[sid] = MCPClient(
                sid,
                spec.get("command"),
                spec.get("args"),
                spec.get("env"),
                enabled=spec.get("enabled", True),
                name=spec.get("name"),
                kind=spec.get("kind"),
                toolkit=toolkit,
            )
        log(f"configured {len(self.clients)} MCP server(s): {', '.join(self.clients) or '(none)'}", "cy")

    def start_all(self):
        # Launch every configured server IN PARALLEL, not one after another.
        # client.start() can block for up to ~12s (its own "wait for Studio's
        # tools to appear" grace loop) - with a sequential for-loop, Roblox
        # being first in config.json meant every OTHER server (Blender, any
        # addon) didn't even begin launching until Roblox's grace loop gave
        # up, even though that addon has nothing to do with Roblox and could
        # have been ready in 1-2s. A thread per client removes that
        # dependency entirely: a slow/absent Roblox Studio no longer holds up
        # an addon server the user actually wants right now.
        threads = []
        for sid, client in self.clients.items():
            if not client.enabled:
                log(f"[{sid}] disabled - not starting", "dim")
                continue
            def _run(sid=sid, client=client):
                try:
                    client.start()
                except Exception as e:
                    log(f"[{sid}] failed to start: {e}  (other servers continue)", "rd")
            t = threading.Thread(target=_run, daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        self.rebuild_index()

    def rebuild_index(self):
        """Aggregate server tools. Collisions get a 'server/' prefix."""
        with self.index_lock:
            self.index = {}
            for sid, client in self.clients.items():
                for t in (client.tools_cache or []):
                    name = t.get("name")
                    if not name:
                        continue
                    advertised = name if name not in self.index else f"{sid}/{name}"
                    self.index[advertised] = (client, name)

    def list_tools(self, refresh=False):
        if refresh:
            for sid, client in self.clients.items():
                try:
                    if not client.is_alive():
                        client.start()
                    else:
                        client.refresh_tools()
                except Exception as e:
                    log(f"[{sid}] refresh failed: {e}", "yl")
            self.rebuild_index()
        out = []
        for sid, client in self.clients.items():
            for t in (client.tools_cache or []):
                name = t.get("name")
                advertised = name
                with self.index_lock:
                    # find the advertised key that maps to this (client, name)
                    for k, (holder, real) in self.index.items():
                        if holder is client and real == name:
                            advertised = k
                            break
                tt = dict(t)
                tt["name"] = advertised
                tt["server"] = sid
                out.append(tt)
        return out

    def call(self, name, arguments, timeout):
        with self.index_lock:
            entry = self.index.get(name)
        if entry is None:
            # Maybe a freshly added tool - rebuild once and retry.
            self.rebuild_index()
            with self.index_lock:
                entry = self.index.get(name)
        if entry is None:
            raise RuntimeError(f"unknown tool '{name}'")
        holder, real_name = entry
        return holder.call_tool(real_name, arguments, timeout)

    def restart(self, server_id=None):
        targets = [self.clients[server_id]] if server_id and server_id in self.clients else list(self.clients.values())
        for client in targets:
            try:
                client.restart()
            except Exception as e:
                log(f"[{client.id}] restart failed: {e}", "rd")
        self.rebuild_index()

    def health(self):
        return [{
                    "id": sid,
                    "name": c.name,
                    "kind": c.kind,
                    "toolkit": c.toolkit,
                    "enabled": c.enabled,
                    "alive": c.is_alive(),
                    "tools": len(c.tools_cache) if c.enabled else 0,
                }
                for sid, c in self.clients.items()]

    def set_toolkit_skill(self, server_id, skill_id, enabled):
        client = self.clients.get(server_id)
        if not client:
            return False, f"unknown MCP server: {server_id}"
        skills = client.toolkit.get("skills") if isinstance(client.toolkit, dict) else None
        if not isinstance(skills, list) or not any(isinstance(s, dict) and s.get("id") == skill_id for s in skills):
            return False, f"unknown toolkit skill for {server_id}: {skill_id}"
        for skill in skills:
            if isinstance(skill, dict) and skill.get("id") == skill_id:
                skill["enabled"] = bool(enabled)
        return True, None

    def any_alive(self):
        return any(c.is_alive() for c in self.clients.values())


# ══════════════════════════════════════════════════════════════════════════
#  WEBSOCKET SERVER
# ══════════════════════════════════════════════════════════════════════════
mgr = MCPManager()
clients = set()

# ── Studio connectivity probe ──────────────────────────────────────────────
# The MCP server process stays alive even when Roblox Studio is closed or its
# MCP option is disabled - tool calls then return instantly with an "Unable to
# find an active Studio instance" text. So "mcp_alive" alone is misleading.
#
# TWO LEVELS (validated live 2026-06):
#  - list_roblox_studios: instant, side-effect-free. studios == [] means NO Studio
#    is connected to the MCP (app closed, OR its "Studio as MCP Server" option is
#    disabled - the two are indistinguishable at this layer). A non-empty list
#    means a Studio app IS connected, BUT note its entry stays present (active:true)
#    even when no place is open - only its "name" goes null. So presence != usable.
#  - get_studio_state: tells whether a PLACE is actually loaded. With a place open
#    it returns "Available DataModels: ..."; with the Studio on the home screen (or
#    the active place closed) it returns "...doesn't have a place opened / previously
#    active Studio has disconnected". That is the authoritative "place loaded" signal
#    (same phrase the call path already recognises in core/main.js).
STUDIO_PROBE_TOOL = "list_roblox_studios"
STUDIO_STATE_TOOL = "get_studio_state"
# Substrings get_studio_state emits when a Studio is connected but no place is open.
NO_PLACE_MARKERS = ("doesn't have a place", "no place opened", "place opened",
                    "has disconnected", "no active studio")


def _probe_tool_text(tool):
    """Call a side-effect-free probe tool with no args; return its text, or None if
    the tool is unavailable / the server is busy / it errored (best-effort)."""
    with mgr.index_lock:
        entry = mgr.index.get(tool)
    if entry is None:
        return None
    holder, real_name = entry
    # Never queue behind a long-running tool call (the probe is best-effort).
    if not holder.call_lock.acquire(blocking=False):
        return None
    try:
        if not holder.is_alive():
            return None
        msg = holder._request("tools/call", {"name": real_name, "arguments": {}}, timeout=8)
        if not msg or msg.get("error"):
            return None
        content = msg.get("result", {}).get("content", [])
        return "\n".join(it.get("text", "") for it in content if it.get("type") == "text")
    except Exception:
        return None
    finally:
        holder.call_lock.release()


def probe_studio():
    """Legacy status contract backed by Monolith readiness.

    app/place giữ tên cũ để các extension 1.5.x không phải đổi wire protocol.

    True:
        Monolith proxy + HTTP backend trong Unreal Editor đang usable.

    False:
        primary proxy chết/missing, hoặc Monolith backend báo MCP tool error.

    None:
        probe tạm bận hoặc chưa có index/tool catalogue.
    """

    # Codex Free has no Studio-specific readiness probe. The stdio MCP child
    # itself is the readiness signal; keep the legacy `studio` fields in the
    # websocket contract so the browser extension remains wire-compatible.
    primary = mgr.clients.get(PRIMARY_SERVER_ID)
    if primary is not None:
        alive = bool(primary.is_alive() and primary.tools_cache)
        return {"app": alive, "place": alive}

    if (
        primary is None
        or not primary.is_alive()
    ):
        return {
            "app": False,
            "place": False,
        }

    # Unreal_mcp keeps its stdio process alive while the native UE bridge is
    # being attached and exposes one gateway tool named `unreal`; it does not
    # implement the old Roblox/Monolith `monolith_status` probe. Treat the
    # gateway as the readiness signal so the extension can show the correct
    # state and route calls to Unreal.
    if primary.kind == "unreal-engine":
        with mgr.index_lock:
            unreal_entry = mgr.index.get("unreal")
        if unreal_entry is None:
            return {"app": None, "place": None}
        return {"app": True, "place": True}

    with mgr.index_lock:
        entry = mgr.index.get(
            "monolith_status"
        )

    if entry is None:
        return {
            "app": None,
            "place": None,
        }

    holder, real_name = entry

    # Không xếp health probe sau một tool call dài.
    if not holder.call_lock.acquire(
        blocking=False
    ):
        return {
            "app": None,
            "place": None,
        }

    try:
        if not holder.is_alive():
            return {
                "app": False,
                "place": False,
            }

        msg = holder._request(
            "tools/call",
            {
                "name": real_name,
                "arguments": {},
            },
            timeout=5,
        )

        if msg is None:
            return {
                "app": None,
                "place": None,
            }

        if msg.get("error"):
            return {
                "app": False,
                "place": False,
            }

        result = (
            msg.get("result", {})
            or {}
        )

        if result.get("isError"):
            return {
                "app": False,
                "place": False,
            }

        return {
            "app": True,
            "place": True,
        }

    except Exception:
        return {
            "app": None,
            "place": None,
        }

    finally:
        holder.call_lock.release()



def safe_call(name, arguments, timeout):
    """Never raises. Preserve MCP tool-level failures as failures."""
    try:
        result = mgr.call(
            name,
            arguments,
            timeout,
        )

        if result.get("is_error"):
            return {
                "ok": False,
                "error": (
                    result.get("text")
                    or f"Tool '{name}' reported an error."
                ),
                "kind": "tool_error",
            }

        return {
            "ok": True,
            "text": result.get("text", ""),
            "images": result.get("images", []),
        }

    except TimeoutError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "kind": "timeout",
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "kind": type(exc).__name__,
        }



async def run_tool_task(ws, name, args, timeout, rid):
    """Execute one tool off the socket read loop and send its result back.

    Kept as a standalone task (not awaited inline in handler) so a long tool
    never starves the connection's ability to answer app-level pings - see the
    call_tool branch in handler() for the full rationale."""
    t0 = time.monotonic()
    res = await asyncio.to_thread(safe_call, name, args, timeout)
    elapsed = time.monotonic() - t0
    tag = "gr" if res.get("ok") else "rd"
    summary = (res.get("text") or res.get("error") or "")[:80].replace("\n", " ")
    slow = "  [SLOW]" if elapsed > 5 else ""
    # Routine per-call traces are technical noise for a non-dev user watching
    # the console; they still land in bridge_debug.log. A failed/slow call
    # DOES surface on the terminal - that's the signal a user should notice.
    log(f"<- {name} ({elapsed:.1f}s){slow}: {summary}", tag, terminal=not res.get("ok") or elapsed > 5)
    try:
        await ws.send(json.dumps({"type": "tool_result", "id": rid, **res}))
    except websockets.ConnectionClosed:
        pass


async def broadcast_status():
    """Push a fresh status snapshot to every currently-connected extension tab.

    Needed because the socket now starts listening (see _boot_and_diagnose in
    main()) before every MCP server has necessarily finished launching in the
    background - an extension that connects in that window gets an early,
    incomplete "connected" snapshot (e.g. an addon server not started yet).
    The extension's own periodic poll only reads a passively cached copy of
    the LAST message it received (background.js never re-probes on its own),
    so without a follow-up push that stale snapshot can persist forever (seen
    live 2026-07-11: Blender not yet alive at connect-time froze the "Start
    Roblox agent" button in its fully-disabled, non-degraded state even long
    after Blender was actually up). background.js already handles a second
    "connected" message arriving at any time (updates its cache and re-renders
    the bar), so re-sending this exact shape once startup truly settles is
    enough to self-correct with zero extension-side changes needed.
    """
    if not clients:
        return
    try:
        _st = await asyncio.to_thread(probe_studio)
        _proc = _st["app"]
        payload = json.dumps({
            "type": "connected",
            "mcp_alive": mgr.any_alive(),
            "studio": _st["place"], "studio_app": _st["app"],
            # Whether a Roblox Studio WINDOW process exists at all - lets the
            # extension word the corrective step correctly ("open the MCP
            # panel in your already-open Studio" vs "launch Studio").
            "studio_proc": _proc,
            "servers": mgr.health(),
            "tools": mgr.list_tools(),
            "port": PORT,
        })
    except Exception:
        return
    for ws in list(clients):
        try:
            await ws.send(payload)
        except Exception:
            pass


async def handler(ws):
    peer = getattr(ws, "remote_address", ("?",))[0]
    clients.add(ws)
    log(f"extension connected  ({peer})  [{len(clients)} client(s)]", "gr")
    try:
        _st = await asyncio.to_thread(probe_studio)
        await ws.send(json.dumps({
            "type": "connected",
            "mcp_alive": mgr.any_alive(),
            "studio": _st["place"], "studio_app": _st["app"],
            "studio_proc": _st["app"],
            "servers": mgr.health(),
            "tools": mgr.list_tools(),
            "port": PORT,
        }))
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mtype = msg.get("type")
            rid = msg.get("id")

            if mtype == "ping":
                await ws.send(json.dumps({"type": "pong", "id": rid}))

            elif mtype == "studio_status":
                studio = await asyncio.to_thread(probe_studio)
                await ws.send(json.dumps({
                    "type": "studio_status", "id": rid,
                    "studio": studio["place"], "studio_app": studio["app"],
                    "studio_proc": studio["app"],
                    "mcp_alive": mgr.any_alive(),
                }))

            elif mtype == "list_tools":
                try:
                    tools = await asyncio.to_thread(mgr.list_tools, True)
                except Exception as e:
                    tools = mgr.list_tools()
                    log(f"list_tools error: {e}", "yl")
                _st = await asyncio.to_thread(probe_studio)
                await ws.send(json.dumps({
                    "type": "tools", "id": rid,
                    "tools": tools, "mcp_alive": mgr.any_alive(),
                    "studio": _st["place"], "studio_app": _st["app"],
                    "studio_proc": _st["app"],
                    "servers": mgr.health(),
                }))

            elif mtype == "call_tool":
                name = msg.get("name", "")
                args = msg.get("arguments") or {}
                timeout = float(msg.get("timeout", 120000)) / 1000.0
                log(f"-> tool  {name}({', '.join(args.keys())})", "cy", terminal=False)
                # Run the tool as a BACKGROUND task instead of awaiting it here.
                # Awaiting inline parks this read loop for the WHOLE tool call, so
                # a long tool (e.g. wait_job_finished > 25s) means the client's
                # app-level pings are never read/answered - its half-open-socket
                # watchdog then force-closes the connection and the in-flight call
                # is dropped as "bridge unreachable" (reported live). As a task,
                # the loop stays free to answer pings/status while the tool runs.
                # The extension only ever has ONE call_tool in flight (its agent
                # loop awaits each result before sending the next), so this never
                # overlaps tool executions.
                asyncio.create_task(run_tool_task(ws, name, args, timeout, rid))

            elif mtype in ("add_server", "remove_server"):
                # Adding/removing an addon MCP server rewrites config.json, which
                # the bridge only reads at launch - so we ack, then restart the
                # whole process to pick it up cleanly. The primary Roblox server
                # is protected inside config_add/remove_server.
                if mtype == "add_server":
                    ok, err = await asyncio.to_thread(
                        config_add_server,
                        msg.get("server_id"), msg.get("command"),
                        msg.get("args"), msg.get("env"))
                else:
                    ok, err = await asyncio.to_thread(
                        config_remove_server, msg.get("server_id"))
                await ws.send(json.dumps({
                    "type": "server_changed", "id": rid,
                    "ok": ok, "error": err, "restarting": ok,
                }))
                if ok:
                    # Give the ack a beat to flush over the socket, then restart.
                    async def _do_restart():
                        await asyncio.sleep(0.4)
                        restart_self()
                    asyncio.create_task(_do_restart())

            elif mtype == "set_server_enabled":
                ok, err = await asyncio.to_thread(
                    config_set_server_enabled,
                    msg.get("server_id"),
                    msg.get("enabled"),
                )
                await ws.send(json.dumps({
                    "type": "server_changed", "id": rid,
                    "ok": ok, "error": err, "restarting": ok,
                }))
                if ok:
                    async def _do_restart_enabled():
                        await asyncio.sleep(0.4)
                        restart_self()
                    asyncio.create_task(_do_restart_enabled())

            elif mtype == "set_toolkit_skill":
                sid = (msg.get("server_id") or "").strip()
                skill_id = (msg.get("skill_id") or "").strip()
                enabled = bool(msg.get("enabled"))
                ok, err = await asyncio.to_thread(
                    config_set_toolkit_skill, sid, skill_id, enabled)
                if ok:
                    ok, err = mgr.set_toolkit_skill(sid, skill_id, enabled)
                await ws.send(json.dumps({
                    "type": "toolkit_changed", "id": rid,
                    "ok": ok, "error": err, "server_id": sid,
                    "skill_id": skill_id, "enabled": enabled,
                    "servers": mgr.health(),
                }))

            elif mtype == "restart_mcp":
                sid = msg.get("server")
                try:
                    await asyncio.to_thread(mgr.restart, sid)
                    ok, err = True, None
                except Exception as e:
                    ok, err = False, str(e)
                await ws.send(json.dumps({
                    "type": "mcp_status", "id": rid,
                    "alive": mgr.any_alive(), "ok": ok, "error": err,
                    "servers": mgr.health(), "tools": mgr.list_tools(),
                }))

            else:
                await ws.send(json.dumps({
                    "type": "error", "id": rid,
                    "error": f"unknown message type: {mtype}",
                }))
    except websockets.ConnectionClosed:
        pass
    except Exception as e:
        log(f"handler error: {e}", "rd")
    finally:
        clients.discard(ws)
        log(f"extension disconnected  [{len(clients)} client(s)]", "yl")


async def server_watch():
    """Poll every MCP server and restart any that died unexpectedly (e.g. the
    StudioMCP proxy crashing on its own - see stop()'s taskkill /T fix and the
    stderr logging above for why this used to happen silently). Without this,
    a dead server only got noticed on the NEXT real tool call, which is what
    made "Studio looks connected but nothing responds" possible."""
    # Crash-LOOP detection thresholds: LOOP_N deaths within LOOP_WINDOW seconds
    # means something is killing (or instantly crashing) the server every time
    # we bring it back - the silent restart cycle the auto-restart otherwise
    # hides completely. We still keep restarting (the cause may be transient,
    # e.g. the user is about to start Blender), but the terminal now NAMES the
    # problem: exit code, the child's last stderr lines, and - for a port-bound
    # server - who is squatting the port. Banner re-prints at most every
    # LOOP_WARN_COOLDOWN so the terminal stays readable.
    LOOP_N = 3
    LOOP_WINDOW = 60
    LOOP_WARN_COOLDOWN = 120
    while True:
        await asyncio.sleep(5)
        for sid, client in list(mgr.clients.items()):
            try:
                if not client.enabled:
                    # A disabled server must stay stopped; otherwise the health
                    # watcher would immediately undo the user's toggle.
                    if client.is_alive():
                        client.stop()
                    continue
                if not client.is_alive():
                    now = time.time()
                    # restart_times holds RESTART ATTEMPTS (appended just before
                    # each start below), never per-poll sightings - appending on
                    # every 5s poll would keep the window full forever and the
                    # "slow down" branch would then block restarts permanently.
                    client.restart_times = [t for t in client.restart_times if now - t < LOOP_WINDOW]
                    looping = len(client.restart_times) >= LOOP_N
                    if looping and now - client.loop_warned_at > LOOP_WARN_COOLDOWN:
                        client.loop_warned_at = now
                        log(f"[{sid}] CRASH LOOP: died {len(client.restart_times)} times in the last "
                            f"{LOOP_WINDOW}s (last exit code: {client.last_exit}). Something is killing it "
                            f"or it cannot start.", "rd")
                        if client.start_error:
                            log(f"[{sid}] {client.start_error}", "rd")
                        elif client.stderr_tail:
                            log(f"[{sid}] last error output (usually the real reason):", "rd")
                            for ln in client.stderr_tail:
                                log(f"[{sid}]   {ln}", "yl")
                        else:
                            log(f"[{sid}] the server printed no error output before dying.", "yl")
                        # Port forensics: name the process squatting a port this
                        # server needs. For the primary Roblox proxy that is
                        # Studio's MCP port; a squatter there (seen live: a
                        # 'ropilot' app) makes the proxy die/misbehave forever.
                        if sid == "roblox":
                            owner = _port_owner(STUDIO_MCP_PORT)
                            if owner:
                                pid, name, path = owner
                                if "roblox" not in (name or "").lower() and "studio" not in (path or "").lower():
                                    log(f"[{sid}] port {STUDIO_MCP_PORT} is held by '{name}' (pid {pid}, {path}) - "
                                        f"close that program, it is squatting Studio's MCP port.", "rd")
                        log(f"[{sid}] common causes: its app is not running (e.g. Blender + addon), a port "
                            f"conflict, an antivirus killing it, or a bad command in config.json. "
                            f"Auto-restart continues in the background.", "yl")
                    if looping and client.restart_times and now - client.restart_times[-1] < 15:
                        # Clearly hopeless right now: drop to a ~15s cadence so a
                        # broken command isn't hammer-spawned every 5 seconds,
                        # while still retrying forever (the cause may clear, e.g.
                        # the user finally opens Blender).
                        continue
                    client.restart_times.append(now)
                    log(f"[{sid}] found dead - auto-restarting...", "yl")
                    await asyncio.to_thread(client.start)
                    mgr.rebuild_index()
                    await broadcast_status()  # tell any connected extension right away
            except Exception as e:
                log(f"[{sid}] auto-restart failed: {e}", "rd")

            # --- tools_dirty: proxy → bridge push ---
            if getattr(client, "tools_dirty", False):
                try:
                    await asyncio.to_thread(client.refresh_tools)
                    mgr.rebuild_index()
                    await broadcast_status()
                    log(
                        f"[{sid}] tools refreshed ({len(client.tools_cache)} tools)",
                        "gr",
                    )
                except Exception as e:
                    log(f"[{sid}] dirty-refresh failed: {e}", "yl")


async def monolith_watch(initial_app, initial_place=None):
    """Monolith backend watcher. Polls backend health and logs state transitions."""
    primary_client = mgr.clients.get(PRIMARY_SERVER_ID)
    if primary_client is None or not getattr(primary_client, "enabled", True):
        return
    prev_state = initial_app
    while True:
        await asyncio.sleep(4)
        try:
            st = await asyncio.to_thread(probe_studio)
        except Exception:
            continue
        state = st["app"]
        if state is not None and state != prev_state:
            if state is True:
                primary = mgr.clients.get(PRIMARY_SERVER_ID)
                total = len(primary.tools_cache) if primary else 0
                log(f"Monolith MCP (Unreal Engine) connected - {total} tools ready.", "gr")
            else:
                log("Monolith MCP (Unreal Engine) disconnected or unreachable.", "yl")
            prev_state = state
            await broadcast_status()



async def _supervised(name, coro_factory):
    """Run a watcher coroutine forever, restarting it if it ever raises.

    Both watchers are designed to never raise, but one line proved that wrong
    in practice (an UnboundLocalError killed studio_watch SILENTLY - asyncio
    only prints 'Task exception was never retrieved' at shutdown, so all
    Studio monitoring and status broadcasts just stopped until the user
    restarted the bridge). A crash in a watcher must never be silent or
    permanent: log it loudly, wait a beat, start a fresh instance.
    """
    while True:
        try:
            await coro_factory()
            return  # normal completion (doesn't happen today, but respect it)
        except Exception as e:
            log(f"{name} crashed: {type(e).__name__}: {e} - restarting it in 5s "
                f"(please report this).", "rd")
            await asyncio.sleep(5)


async def main():
    print(f"\n{C['cy']}  Codex Free Batch Bridge v{BRIDGE_VERSION}{C['reset']}  {C['dim']}- multi-MCP WebSocket bridge - ws://{HOST}:{PORT}{C['reset']}\n")
    log(f"===== BRIDGE START  v{BRIDGE_VERSION}  pid={os.getpid()}  log={LOG_PATH} =====", "cy")
    killed_squatter = False
    mgr.load_config()

    # Shared "we already told the user the corrective step" flag. Two producers
    # can print the 'toggle Studio's MCP server' action banner: the early
    # _early_studio_guidance task (fast, doesn't wait for start_all's ~48s grace
    # loop) and the post-start_all diagnostic block in _boot_and_diagnose. This
    # flag lets whichever fires first suppress the other, so the user never sees
    # the same instruction twice. Mutable dict so both nested coroutines share it.
    _guidance_shown = {"v": False}

    async def _early_studio_guidance():
        """Print the corrective action banner WITHOUT waiting for start_all()'s
        full ~48s grace loop.

        RobloxClient.start() retries tools/list for up to ~48s to catch a Studio
        that attaches a little late - correct for a Studio that IS coming, but it
        also means a user whose Studio is simply closed or whose MCP toggle is off
        waits ~48s before the terminal tells them what to do (the extension, which
        reads status over the socket, already says it immediately). So after a
        short grace we check independently and, if still not connected, show the
        step now. If Studio then attaches, studio_watch prints the green
        'connected' line - so an early banner is at worst redundant, never wrong
        (the user confirmed a premature toggle hint during boot is harmless).

        Deliberately does NOT try to distinguish 'Studio closed' from 'MCP toggle
        off': probe_studio can't tell them apart (both read app=False, see its
        docstring), and the banner wording already covers both, so there is no
        finer detection to preserve here. A proven port-squatter case is left to
        _boot_and_diagnose / studio_watch, which print their own, more specific
        hint."""
        if PRIMARY_SERVER_ID not in mgr.clients:
            return
        await asyncio.sleep(12)  # give a fast, normal attach the chance to win
        if _guidance_shown["v"]:
            return
        rc = mgr.clients.get(PRIMARY_SERVER_ID)
        if rc is None or not getattr(rc, "enabled", True) or getattr(rc, "saw_foreign_ws_host", False):
            return
        if rc.tools_cache:
            # Tools present - either connected, or an attach is mid-flight; defer
            # to the authoritative probe / studio_watch rather than second-guess.
            st = await asyncio.to_thread(probe_studio)
            if st["app"] is not False:
                return
        _guidance_shown["v"] = True
        action_banner([
            "Open your place in Roblox Studio.",
            "Go to: Assistant Settings > MCP Servers",
            "       > 'Enable Studio as MCP server'",
            "It can take up to ~10s; this window will turn green.",
        ])

    async def _boot_and_diagnose():
        """Launch every configured MCP server and print the boot diagnostic
        banner. Runs as a background task AFTER the socket below is already
        listening, so a slow or absent Roblox Studio never delays the
        extension's ability to connect and use OTHER MCP servers (e.g.
        Blender) right away - only the terminal banner and Roblox's own
        auto-recovery loop wait on this. (mgr.start_all() itself also
        launches every server in parallel now, for the same reason.)"""
        try:
            await asyncio.to_thread(mgr.start_all)
        except Exception as e:
            log(f"server startup error: {e}", "rd")
            log("The bridge will keep running; it retries on the first tool call.", "yl")
        total = len(mgr.list_tools())
        # Roblox-only count for the corrective message below: list_tools() sums
        # every configured server (Roblox + addons like Blender), so printing
        # `total` there falsely blamed addon tools on "NO Roblox Studio connected"
        # (seen live: 49 = 27 Roblox + 22 Blender, message only about Roblox).
        roblox_client = mgr.clients.get("roblox")
        roblox_total = len(roblox_client.tools_cache) if roblox_client else 0

        # Port-hijack check (ropilot etc.), done at boot: the child's stderr has
        # by now had its ~12s grace loop to reveal it connected to a foreign host
        # on the MCP port. This is proof the port is squatted even when the
        # one-shot check_studio_port() at startup missed it (a background helper
        # grabbing the port a beat after that check ran - seen live 2026-07-13).
        if (roblox_client is not None and roblox_total == 0
                and roblox_client.saw_foreign_ws_host):
            killed, sname = await asyncio.to_thread(_kill_port_squatter)
            if killed:
                try:
                    await asyncio.to_thread(mgr.restart, "roblox")
                    roblox_total = len(roblox_client.tools_cache)
                    total = len(mgr.list_tools())
                except Exception as e:
                    log(f"roblox proxy restart after squatter kill failed: {e}", "rd")
                _print_squatter_hint(sname)

        # Set True if we kill a leftover StudioMCP zombie below and print the
        # re-register hint - so the diagnostic block further down doesn't ALSO
        # print its own near-identical action banner (the same de-duplication
        # killed_squatter already does for the ropilot-squatter path).
        reclaimed_zombie = False
        # Zombie-port deadlock check, done at boot too (not just studio_watch):
        # with Studio ALREADY open, _kill_orphan_studio_mcp was skipped by its
        # safety guard, so a leftover StudioMCP.exe may still own the port and
        # our fresh proxy just spent its 12s grace loop talking to nothing.
        if (roblox_client is not None and roblox_total == 0
                and await asyncio.to_thread(_roblox_studio_app_running) is True
                and await asyncio.to_thread(_reclaim_studio_port, roblox_client)):
            try:
                await asyncio.to_thread(mgr.restart, "roblox")
                roblox_total = len(roblox_client.tools_cache)
                total = len(mgr.list_tools())
            except Exception as e:
                log(f"roblox proxy restart after zombie kill failed: {e}", "rd")
            reclaimed_zombie = True
            _print_reregister_hint()

        # A tool count alone only proves StudioMCP (the proxy) is up - it advertises
        # its catalogue even with NO Studio attached. The authoritative "a Studio is
        # actually connected" signal is the list_roblox_studios probe. So we probe
        # FIRST and only show the green "ready" line when Studio is really attached;
        # otherwise we show just the corrective step (no misleading green success).
        # Probe even when total == 0: StudioMCP advertises an EMPTY catalogue when
        # Studio's MCP server toggle is off (or no place is open), so 0 tools is the
        # most common "needs a corrective step" state, not a success.
        primary_client = mgr.clients.get(PRIMARY_SERVER_ID)
        if primary_client is None or not getattr(primary_client, "enabled", True):
            log(f"ready {total} tools available (default: desktop-commander)", "gr")
            return

        _st = await asyncio.to_thread(probe_studio)
        # Even when Studio (and its place) were ALREADY open before the bridge
        # started, the freshly-launched StudioMCP proxy needs a moment to (re)bind
        # to Studio's own MCP port - so an instant probe right after launch often
        # reads app=False for a beat before flipping True a few seconds later
        # (studio_watch would catch it, but only after printing a scary yellow
        # "not connected" block first). Give it the same grace period the tools
        # probe already gets before deciding it is a real problem.
        if _st["app"] is False:
            with _Spinner("    waiting for Roblox Studio to attach..."):
                for _ in range(8):
                    await asyncio.sleep(1)
                    _st = await asyncio.to_thread(probe_studio)
                    if _st["app"] is not False:
                        break
        # A single app=True reading can be a STALE positive: StudioMCP.exe can
        # answer list_roblox_studios with a leftover studio entry from a PREVIOUS
        # session even though no Studio window is actually open right now (seen
        # live 2026-07-11: bridge booted with Studio fully closed, still printed
        # "Roblox Studio connected" from the very first probe). studio_watch
        # already distrusts a single reading for PLACE transitions the same way -
        # apply the identical confirm-before-trusting step here for APP, so the
        # boot banner can't announce a connection that isn't really there.
        if _st["app"] is True:
            await asyncio.sleep(1.5)
            confirm = await asyncio.to_thread(probe_studio)
            if confirm["app"] is not True:
                _st = confirm
        if roblox_client is not None and (roblox_total == 0 or _st["app"] is False):
            if killed_squatter or reclaimed_zombie or _guidance_shown["v"]:
                # The action banner was ALREADY shown - either right after a kill
                # (check_studio_port / _print_reregister_hint for a zombie) or by
                # the early _early_studio_guidance task. Repeating the full
                # explanation here in a different color, seconds later, reads as a
                # second unrelated problem to a non-technical user (seen live
                # 2026-07-13: the toggle instruction and this block blurred
                # together). Just confirm we're still waiting, no new instructions.
                log("    still waiting for you to toggle Studio's MCP server "
                    "(see the action box above)...", "yl")
            else:
                _guidance_shown["v"] = True
                # No squatter: Studio is simply closed, or its MCP option is off.
                # Match the exact steps the extension itself tells the user
                # (Assistant Settings > MCP Servers), not a paraphrase - a
                # differently-worded instruction here reads as a second,
                # unrelated problem instead of the same one step.
                if roblox_total > 0:
                    log(f"    {roblox_total} Roblox tools loaded, but NO Roblox Studio is connected yet.", "yl")
                    log("    (This can be a slow attach that clears itself within ~10-15s -", "yl")
                    log("    watch for a green 'Roblox Studio connected' line right after.)", "yl")
                action_banner([
                    "Open your place in Roblox Studio.",
                    "Go to: Assistant Settings > MCP Servers",
                    "       > 'Enable Studio as MCP server'",
                    "It can take up to ~10s; this window will turn green.",
                ])
        elif _st["app"] is True:
            log(f"ready {total} tools available across configured MCP servers", "gr")
        else:
            log(f"ready {total} tools available ({len(mgr.clients)} MCP server(s))", "gr")
        asyncio.create_task(_supervised(
            "monolith_watch", lambda: monolith_watch(_st["app"], _st["place"])))

    async def _early_status_pushes():
        """A few follow-up status broadcasts shortly after boot.

        mgr.start_all() still doesn't RETURN until every server's thread has
        joined - including Roblox's, which can take up to ~48s (StudioMCP's
        own internal "waiting for tools" retry loop, seen live). So a single
        broadcast placed after start_all() would be just as slow as the old
        blocking behavior for the exact case this is meant to fix: an addon
        server (e.g. Blender) that's ready in 1-13s while Roblox is still
        slowly timing out. Poll-and-broadcast a few times instead, cheaply,
        so any extension that connected during that window self-corrects
        quickly instead of staying stuck on its first, incomplete snapshot.
        """
        for interval in (2, 2, 4, 6, 6):  # cumulative: 2s, 4s, 8s, 14s, 20s after boot
            await asyncio.sleep(interval)
            await broadcast_status()

    # Free our own port from a leftover bridge (double-launch / X-closed window /
    # prior crash) BEFORE binding, so relaunching start.bat "just works" instead
    # of dying on WinError 10048. Only ever kills a proven bridge.py; anything
    # else falls through to the friendly bind-error below.
    if await asyncio.to_thread(_reclaim_bridge_port):
        await asyncio.sleep(0.6)  # let Windows release the socket before we bind

    try:
        server_ctx = await websockets.serve(
            handler, HOST, PORT, ping_interval=20, ping_timeout=20,
            max_size=16 * 1024 * 1024)
    except OSError as e:
        # errno 10048 (Win) / EADDRINUSE: something we could NOT auto-kill still
        # owns the port - another app, or a python whose cmdline we couldn't read.
        if getattr(e, "errno", None) in (98, 10048) or "10048" in str(e):
            owner = await asyncio.to_thread(_port_owner, PORT)
            who = f" by '{owner[1]}' (pid {owner[0]})" if owner else ""
            log(f"could not start: port {PORT} is already in use{who}.", "rd")
            log(f"    A previous bridge may still be running, or another app took "
                f"the port. Close it, then relaunch. To find it:", "yl")
            log(f"      netstat -ano | findstr {PORT}", "yl")
            log(f"      taskkill /F /PID <the pid from the last column>", "yl")
            log(f"    Or set a different port before start.bat:  set ZS_BRIDGE_PORT=17614", "yl")
            return
        raise

    async with server_ctx:
        log(f"listening on ws://{HOST}:{PORT}  - load the extension and open a supported AI chat", "cy")
        asyncio.create_task(_supervised("server_watch", server_watch))
        asyncio.create_task(_boot_and_diagnose())
        asyncio.create_task(_early_studio_guidance())
        asyncio.create_task(_early_status_pushes())
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("shutting down...", "yl")
        for c in mgr.clients.values():
            c.stop()
    finally:
        log("===== BRIDGE STOP =====", "cy")
