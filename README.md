# Surface Laptop 2 — OV9734 Webcam Fix

Gets the built-in webcam working on **Surface Laptop 2 (Model 1769)** running
the [linux-surface](https://github.com/linux-surface/linux-surface) kernel on
Ubuntu/Debian-based distributions (tested on POP!_OS 24.04).

The camera will appear as **"Surface Laptop 2 Webcam"** in PipeWire and works
with Cheese, browsers (via xdg-desktop-portal), OBS, and any other PipeWire
Video/Source consumer. The camera light only turns on when an application
actively requests the camera, and turns off automatically when it disconnects.

---

## Requirements

| Requirement | Notes |
|-------------|-------|
| Hardware | Surface Laptop 2 (Model 1769) |
| Kernel | [linux-surface](https://github.com/linux-surface/linux-surface) kernel (`*-surface-*`) |
| Audio session | PipeWire + WirePlumber (default on Ubuntu 22.04+ / POP!_OS) |
| Build tools | `dkms`, `linux-headers-$(uname -r)` (installed automatically) |

---

## Why this is needed — the three-layer problem

The stock kernel has three separate bugs that each prevent the camera working:

### Layer 1 — ipu_bridge doesn't know about this sensor
`ipu_bridge` (the Intel IPU3 camera bridge driver) maintains a list of
supported sensors. The OV9734 used in the Surface Laptop 2 (`OVTI9734` ACPI
ID) is not in that list, so the bridge never creates the software fwnode that
connects the sensor to the IPU3 pipeline.

**Fix:** DKMS module `ipu-bridge-ov9734` replaces `ipu_bridge.ko` with a
version that includes `OVTI9734`.

### Layer 2 — probe ordering race condition
`ov9734` probes over I²C before `ipu3_cio2` has loaded. At that point
`ipu_bridge_init()` hasn't run yet, so no fwnode exists and `ov9734_probe()`
fails with `-ENODEV`.

**Fix:** A udev rule triggers when the IPU3 CIO2 PCI device (`8086:9d32`)
binds, then immediately rebinds `ov9734` so it probes again after the fwnode
exists.

> **Note:** Do NOT rebind `INT3472:00` or `INT3472:01`. The `int3472-discrete`
> driver calls `acpi_dev_clear_dependencies()` which is one-shot — rebinding
> it after boot will always fail and requires a reboot to recover.

### Layer 3 — sensor never gets its clock or reset signal
The ACPI firmware for this machine exposes two GPIOs via `INT3472:01`:
- **GPIO 120** → clock enable (MCLK to the sensor)
- **GPIO 77** → reset (active-low)

The stock `ov9734` driver never asserts these, so the sensor stays powered
down and all I²C transactions return `-EREMOTEIO`.

**Fix:** DKMS module `ov9734-surface` replaces `ov9734.ko` with a patched
version that calls `clk_prepare_enable()` (asserts GPIO 120) and
`gpiod_set_value(reset, 0)` (deasserts GPIO 77) during probe.

### The bridge — RAW10 → PipeWire
The IPU3 CIO2 captures raw `SGRBG10_1X10` (Bayer 10-bit) frames. No in-kernel
debayer path exists for this format, so a userspace bridge script
(`camera-bridge.py`) reads raw frames, debayers them to RGB, and publishes
them as a **PipeWire Video/Source** node.

The bridge uses `pw-dump` to watch for real PipeWire consumer links. The
sensor only starts capturing when an application actually connects; it stops
~5 seconds after the last application disconnects.

---

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/surface-laptop-2-camera.git
cd surface-laptop-2-camera
sudo ./install.sh   # Or ./install-arch.sh for Arch users
```

Then **reboot**.

After rebooting:

```bash
# Check the bridge is running
systemctl --user status camera-bridge.service

# Test the camera
cheese
```

---

## Uninstalling

```bash
cd surface-laptop-2-camera
./uninstall.sh   # no sudo needed — it will prompt when required
```

Then reboot.

---

## Troubleshooting

### Camera not found in apps after reboot

```bash
systemctl --user status camera-bridge.service
```

If the service is not running:
```bash
systemctl --user start camera-bridge.service
journalctl --user -u camera-bridge.service -n 50
```

### "Could not find ipu3-cio2 1 video node"

The udev rebind may not have fired. Check:
```bash
dmesg | grep -E "ov9734|ipu3|OVTI"
ls /dev/media*
media-ctl -d /dev/media0 -p | grep -E "ov9734|cio2"
```

If `ov9734 2-0036` is not in the media graph, trigger a manual rebind:
```bash
sudo /usr/local/sbin/ipu3-camera-rebind.sh
```

### Camera light stays on / sensor never turns off

Check whether another application holds the camera open:
```bash
pw-dump | python3 -c "
import json,sys
data=json.load(sys.stdin)
links=[o for o in data if o.get('type')=='PipeWire:Interface:Link']
print(f'{len(links)} PipeWire links active')
for l in links: print(' ', l.get('info',{}).get('output-node-id'), '->', l.get('info',{}).get('input-node-id'))
"
```

### Firefox can't detect the camera

Firefox may not have pipewire cameras enabled by default. Navigate to `about:config`,
and set `media.webrtc.camera.allow-pipewire` to `true`. Then restart firefox.

### Green / colour-shifted image

The debayer white balance coefficients in `camera-bridge.py` are tuned for
the Surface Laptop 2. If colours look wrong, adjust the multipliers in the
`debayer_grbg_half()` function:
```python
r = np.clip(r * 2.0, 0, 255)   # increase to warm up
g = np.clip(g * 1.0, 0, 255)
b = np.clip(b * 1.8, 0, 255)   # increase to cool down
```

---

## File layout (installed)

| Path | Purpose |
|------|---------|
| `/usr/src/ipu-bridge-ov9734-1.0/` | Layer 1 DKMS source |
| `/usr/src/ov9734-surface-1.0/` | Layer 3 DKMS source |
| `/usr/local/sbin/ipu3-camera-rebind.sh` | Layer 2 rebind script |
| `/etc/udev/rules.d/99-ov9734-rebind.rules` | udev rule (Layer 2) |
| `/etc/modprobe.d/v4l2loopback.conf` | v4l2loopback parameters |
| `/usr/local/share/surface-laptop-2-camera/camera-bridge.py` | Userspace bridge |
| `~/.config/systemd/user/camera-bridge.service` | Systemd user service |

---

## Tested on

| Distribution | Kernel | Status |
|--------------|--------|--------|
| POP!_OS 24.04 (Noble) | 6.18.7-surface-1 | ✅ Working |

Contributions with test results on other distributions welcome.

---

## Acknowledgements

Developed by reverse-engineering the ACPI DSDT tables (`CAMP`, `INT3472:01`),
tracing `int3472-discrete` GPIO registration, and iterating through `dmesg` and
`media-ctl` topology output to understand exactly why each layer failed.
