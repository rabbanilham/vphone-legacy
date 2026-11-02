"""Pure boot evidence classification. UI matches are conservative and profile specific.

A SpringBoard/lockdownd log proves userspace only. No I/O or QMP requests are
performed here: callers supply serial text and a QMP screendump (P6 PPM bytes).
"""
import json
from pathlib import Path
import re

REFERENCE = Path(__file__).resolve().parents[2] / 'profiles/ui/home-reference.json'


def serial_stage(serial):
    if isinstance(serial, bytes):
        serial = serial.decode('utf-8', errors='replace')
    if (re.search(r'(?m)^[\r ]*panic\(cpu [0-9]+ caller 0x[0-9a-fA-F]+\):', serial) or
            re.search(r'panic\(cpu [0-9]+ caller 0x[0-9a-fA-F]+\):[^\n]*\n[\r ]*Debugger message: panic', serial)):
        return 'panic'
    if re.search(r'SpringBoard|lockdownd|launchd|Darwin Bootstrapper', serial, re.I):
        return 'userspace'
    if re.search(r'Darwin Kernel|AppleARM|BSD root|Kernel version', serial, re.I):
        return 'kernel'
    return 'none'


def read_ppm(data):
    """Parse binary P6, including header comments, without consuming pixel whitespaces."""
    offset = 0
    def token():
        nonlocal offset
        while offset < len(data):
            if data[offset:offset + 1] == b'#':
                end = data.find(b'\n', offset)
                if end == -1:
                    raise ValueError('Truncated PPM comment')
                offset = end + 1
            elif data[offset] in b' \t\r\n':
                offset += 1
            else:
                break
        start = offset
        while offset < len(data) and data[offset] not in b' \t\r\n':
            offset += 1
        return data[start:offset]
    if token() != b'P6':
        raise ValueError('Expected QMP P6 screendump')
    width, height, maximum = int(token()), int(token()), int(token())
    if maximum != 255 or width < 1 or height < 1 or width * height > 16000000:
        raise ValueError('Unsupported PPM geometry/depth')
    if offset >= len(data) or data[offset] not in b' \t\r\n':
        raise ValueError('Missing PPM raster separator')
    offset += 2 if data[offset:offset + 2] == b'\r\n' else 1
    pixels = data[offset:]
    if len(pixels) != width * height * 3:
        raise ValueError('PPM raster size mismatch')
    return width, height, pixels


def thumbnail(width, height, rgb, tw=32, th=56):
    if len(rgb) != width * height * 3:
        raise ValueError('RGB raster size mismatch')
    # Box averaging makes matching independent of QMP resolution and avoids aliasing.
    result = []
    for y in range(th):
        for x in range(tw):
            sums, count = [0, 0, 0], 0
            for sy in range(y * height // th, max(y * height // th + 1, (y + 1) * height // th)):
                for sx in range(x * width // tw, max(x * width // tw + 1, (x + 1) * width // tw)):
                    i = (sy * width + sx) * 3
                    for c in range(3):
                        sums[c] += rgb[i + c]
                    count += 1
            result.extend(v // count for v in sums)
    return result


def framebuffer_stage(width, height, rgb, reference=None):
    if not .54 < width / height < .59:
        return 'none'
    pixels = thumbnail(width, height, rgb)
    if reference is None:
        reference = json.loads(REFERENCE.read_text())['rgb']
    if len(reference) != len(pixels):
        raise ValueError('Reference geometry mismatch')
    # Ignore clock/carrier text; require similarity across all icon rows AND dock.
    bands = ((4, 14), (14, 25), (25, 36), (36, 46), (48, 56))
    errors = [sum(abs(a - b) for a, b in zip(pixels[lo*96:hi*96], reference[lo*96:hi*96])) /
              ((hi-lo)*96*255) for lo, hi in bands]
    if max(errors) < .105:
        return 'home'
    def white_fraction(lo, hi):
        values = [pixels[i:i+3] for i in range(lo * 96, hi * 96, 3)]
        return sum(min(p) > 180 and max(p)-min(p) < 50 for p in values) / len(values)
    # A large white clock/date above empty wallpaper plus a bottom slide label.
    # Never classify a generic wallpaper/status bar or an all-white setup screen.
    clock, middle, slider = (white_fraction(*b) for b in ((5, 14), (18, 42), (45, 53)))
    colorful = sum(max(pixels[i:i+3])-min(pixels[i:i+3]) > 35
                   for i in range(0, len(pixels), 3)) / (32*56)
    if .06 < clock < .45 and middle < .025 and .015 < slider < .22 and colorful > .3:
        return 'lockscreen'
    return 'none'


def classify_boot(serial='', screendump=None, *, reference=None):
    stage = serial_stage(serial)
    if stage == 'panic':
        return stage
    if screendump is not None:
        visual = framebuffer_stage(*read_ppm(screendump), reference=reference)
        if visual != 'none':
            return visual
    return stage


# Short alias for runtime integration.
boot_check = classify_boot
