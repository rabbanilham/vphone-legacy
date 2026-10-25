# T-trackpad-touch: two-finger contact frames and Simulator-style gestures

## Result
- The guest now accepts two simultaneous fingers. Verified live on 2026-10-25:
  - A two-finger QMP `mtt` drag scrolled the Settings list. There was no
    spurious tap.
  - A one-finger MTT swipe unlocked the lock screen.
  - A one-finger MTT tap opened an app.
- The Cocoa window maps host gestures to fingers: trackpad scroll, mouse wheel,
  Option-drag pinch, Option+Shift pan and trackpad magnify. See the
  `backend/README.md` section "Trackpad gestures".
- Pinch has not been visually confirmed in an app. Maps needs network tiles,
  and the guest is offline.

## Report 0x74 header (MultitouchSupport `mt_unpack_contacts` 187ccf8e0)
The header decoder at 187ccf708 copies these frame bytes:

| Bytes | Meaning |
|---|---|
| 2 | Header length (contacts start here) |
| 4..7 | Timestamp |
| 10 | Flags (type) |
| 12..13 | **Total** contact payload bytes |
| 14 | **Contact count** |

- The unpacker computes stride = total / count. It rejects a non-zero
  remainder.
- Strides of 20 bytes or more take the full-record path. Fields past the stride
  are zero-filled or derived.
- Byte 12 was first treated as the stride. With count 2 that decoded two
  15-byte garbage contacts. In Maps this opened the Bookmarks sheet from a
  phantom tap.

## Two-finger frame layout
- Each contact is a 30-byte record at offset `26 + 30*i`.
- `path = slot + 1` and `identity = 2 + i` (both must be fingers, 1..5).
- Single-contact frames are byte-identical to before.
