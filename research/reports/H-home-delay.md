# Home latency investigation (2026-10-27)

Branch: fix/homedelay. Partial diagnosis; no live SpringBoard latency claim.

The toolbar calls `screen.cancelTouch(); rfb.home()`. This sends VNC keysym
0xff50, not QMP. QMP Home injection is used by `prepareControlCenter` only
when the digitizer is gated. Both reach the same `mouse_touch_event` handler.

The player scheduled VNC key-up after 100 ms of host time, and the QMP wake
path slept for 100 ms between down/up. Neither is needed: the backend ignores
Home key-up and owns a 100 ms **virtual-time** pulse. Removed both host waits.
This does not shorten the device pulse or prove a visible speedup: the old
host key-up was already ignored. In particular, virtual time and host time
cannot be compared directly under loaded TCG.

The device asserts GPIO2 immediately on key-down, latches the configured edge,
and calls `touch_gpio_update` / `aic_update` synchronously. No button debounce
or interrupt coalescing timer is present in this path. Its only timer releases
Home 100 ms later. Retained that pulse because shortening it requires native
guest debounce/recognition evidence. Sleep/wake uses the same Home pulse; the
remaining readiness poll is for the digitizer and is not on the toolbar path.

## Measurements

Private backend binary, isolated synthetic ARM guest, three QMP down/up pairs:

- Host command round trips for both commands: 0.937, 0.697, 0.688 ms.
- GPIO2 down-to-up virtual durations from `-d unimp` BUTTON trace:
  101.948, 101.035, 101.783 ms.
- Immediate host key-up still permits the automatic release and GPIO edge latch.

`home_button_contract_test.py` now checks immediate key-up, all three paired
BUTTON traces and the minimum device pulse. These numbers measure transport
and device behavior, not iOS event delivery or presentation.

A primary-checkout guest was already running (PID 59645, observed during this
investigation). It was left untouched to honor the one-guest constraint.
Consequently click-to-dispatch on the native player, guest IRQ consumption,
SpringBoard handling, and first changed frame remain unmeasured. A double-click
recognition window is a hypothesis, not an established dominant cause. Do not
patch guest timers or synthesize double presses on this evidence.

## Manual verification and remaining experiment

1. When the existing guest is stopped, launch this worktree's player/backend.
2. Unlock, open Settings, and click Home; verify a single return to Home.
3. Repeat after sleep: first Home should wake without an unintended second press.
4. Repeat quick presses; verify there is no stuck Home or unexpected Siri.
5. For latency attribution, capture host monotonic click/send/receive times,
   BUTTON virtual timestamps, native SpringBoard button handling, and the first
   changed icon-area frame. Compare 100 ms with shorter guest-time pulses only
   after checking native debounce, wake reliability and double-click behavior.
   Ignore unrelated clock/status-bar frame changes.

If native traces establish a double-click window as dominant, the safest first
mitigation is a documented guest-supported Home-click-speed setting, if this
image offers one. Preserve double-click and accessibility semantics; do not
bypass the recognizer or globally accelerate guest time.

## Validation

Home backend contract passes, including immediate key-up and timer release.
Full unittest discovery: 331 tests, OK, 25 skipped. The shared Swift module
cache initially produced duplicate-module compiler errors; the passing run
used a temporary swiftc wrapper redirecting only `-module-cache-path` to a
private `/tmp` cache, without modifying shared build artifacts. The RFB fixture
now expects key-up immediately after key-down. No backend C change or rebuild.
