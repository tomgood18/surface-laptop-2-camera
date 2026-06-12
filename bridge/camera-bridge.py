#!/usr/bin/env python3
"""
Surface Laptop 2 webcam bridge — PipeWire + V4L2 loopback, idle-aware
ov9734 IPU3 RAW10 (ip3G) → debayer → pipewiresink (node 77, for Cheese)
                                    → /dev/video20 YUYV (for Firefox V4L2
                                      direct access and portal via node 83)

Consumer detection:
  - PipeWire links to node 77 (ov9734-webcam) or node 83 (v4l2loopback)
  - Direct V4L2 readers on /dev/video20 (Firefox bypasses the portal)

/dev/video20 is opened at startup with YUYV format so node 83's format is
stable before any app tries to connect (fixes portal buffer-alloc failures).
"""
import subprocess, signal, sys, os, time, threading, fcntl, select, json, struct
import numpy as np
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

SENSOR_W   = 1296
SENSOR_H   = 734
STRIDE     = 1664
FRAME_IN   = STRIDE * SENSOR_H
OUT_W      = SENSOR_W // 2
OUT_H      = SENSOR_H // 2
FRAMERATE  = 30
STOP_GRACE = 5.0
V4L2_DEV   = '/dev/video20'

# V4L2 constants
_VIDIOC_S_FMT              = 0xc0d05605
_VIDIOC_STREAMON           = 0x40045612   # _IOW('V', 18, int)
_V4L2_BUF_TYPE_OUTPUT      = 2            # writer always uses OUTPUT type regardless of exclusive_caps
_V4L2_PIX_FMT_YUYV         = 0x56595559  # 'YUYV' little-endian
_YUYV_STRIDE               = OUT_W * 2   # 2 bytes per pixel


def find_cio2_video_node():
  out = subprocess.check_output(
      ['media-ctl', '-d', '/dev/media0', '-p'],
      text=True, stderr=subprocess.DEVNULL,
  )
  in_entity = False
  for line in out.splitlines():
      if 'ipu3-cio2 1' in line and 'entity' in line:
          in_entity = True
      if in_entity and 'device node name' in line:
          return line.split()[-1]
  raise RuntimeError('Could not find ipu3-cio2 1 video node')

def find_ov9734_subdev():
    import glob
    for subdev in sorted(glob.glob('/dev/v4l-subdev*')):
        try:
            out = subprocess.check_output(
                ['v4l2-ctl', '-d', subdev, '--list-ctrls'],
                text=True, stderr=subprocess.DEVNULL
            )
            if 'analogue_gain' in out and 'digital_gain' in out:
                return subdev
        except subprocess.CalledProcessError:
            continue
    raise RuntimeError('Could not find ov9734 subdev with analogue_gain and digital_gain controls')

def setup_media_pipeline():
  cmds = [
      ['media-ctl', '-d', '/dev/media0', '-l',
       '"ov9734 2-0036":0->"ipu3-csi2 1":0[1]'],
      ['media-ctl', '-d', '/dev/media0', '--set-v4l2',
       '"ov9734 2-0036":0[fmt:SGRBG10_1X10/1296x734]'],
      ['media-ctl', '-d', '/dev/media0', '--set-v4l2',
       '"ipu3-csi2 1":0[fmt:SGRBG10_1X10/1296x734]'],
      ['media-ctl', '-d', '/dev/media0', '--set-v4l2',
       '"ipu3-csi2 1":1[fmt:SGRBG10_1X10/1296x734]'],
      ['v4l2-ctl', '-d', find_ov9734_subdev(),
       '--set-ctrl=analogue_gain=80,digital_gain=400'],
  ]
  for cmd in cmds:
      subprocess.run(cmd, check=True, capture_output=True)


def open_v4l2_loopback():
  """Open /dev/video20 as writer and fix YUYV format via ioctl.
  Returns the fd, or None if unavailable (no v4l2loopback module)."""
  try:
      fd = os.open(V4L2_DEV, os.O_WRONLY | os.O_NONBLOCK)
  except OSError as e:
      print(f'v4l2loopback not available ({e}), skipping', flush=True)
      return None
  # struct v4l2_format layout (208 bytes total):
  #   offset 0: type (4 bytes)
  #   offset 4: 4 bytes padding (fmt union is 8-byte aligned because
  #             v4l2_window inside the union contains 64-bit pointers)
  #   offset 8: fmt union (200 bytes) — fmt.pix starts here:
  #     +0  width, +4  height, +8  pixelformat, +12 field,
  #     +16 bytesperline, +20 sizeimage, +24 colorspace
  # struct v4l2_format (208 bytes): type@0, padding@4, fmt.pix@8
  # Set both OUTPUT and CAPTURE format to YUYV 648×367 before WirePlumber
  # or Firefox open the device. Without locking CAPTURE here, WirePlumber
  # negotiates MJPG 960×720 independently, causing a format mismatch.
  def _sfmt(buf_type):
      fmt = bytearray(208)
      struct.pack_into('<I', fmt,  0, buf_type)
      struct.pack_into('<I', fmt,  8, OUT_W)
      struct.pack_into('<I', fmt, 12, OUT_H)
      struct.pack_into('<I', fmt, 16, _V4L2_PIX_FMT_YUYV)
      struct.pack_into('<I', fmt, 20, 1)                       # V4L2_FIELD_NONE
      struct.pack_into('<I', fmt, 24, _YUYV_STRIDE)
      struct.pack_into('<I', fmt, 28, _YUYV_STRIDE * OUT_H)
      return fmt
  fmt = _sfmt(_V4L2_BUF_TYPE_OUTPUT)
  try:
      fcntl.ioctl(fd, _VIDIOC_S_FMT, fmt)
  except OSError as e:
      print(f'v4l2loopback VIDIOC_S_FMT failed ({e}), skipping', flush=True)
      os.close(fd)
      return None
  print(f'v4l2loopback: {V4L2_DEV} ready ({OUT_W}×{OUT_H} YUYV)', flush=True)
  return fd


def unpack_ipu3_raw10(buf):
  raw = np.frombuffer(buf, dtype=np.uint8).reshape(SENSOR_H, 52, 32)
  u32 = raw.view('<u4')
  out = np.empty((SENSOR_H, 52, 25), dtype=np.uint8)
  for k in range(25):
      bit = k * 10
      w   = bit >> 5
      b   = bit & 31
      lo  = 32 - b
      if lo >= 10:
          val = (u32[:, :, w] >> b) & 0x3FF
      else:
          hi_mask = (1 << (10 - lo)) - 1
          val = ((u32[:, :, w] >> b) | ((u32[:, :, w + 1] & hi_mask) << lo)) & 0x3FF
      out[:, :, k] = (val >> 2).astype(np.uint8)
  return out.reshape(SENSOR_H, 52 * 25)[:, :SENSOR_W]


def debayer_grbg_half(raw):
  r = raw[0::2, 1::2].astype(np.uint16)
  g = (raw[0::2, 0::2].astype(np.uint16) + raw[1::2, 1::2]) // 2
  b = raw[1::2, 0::2].astype(np.uint16)
  r = np.clip(r * 2.0, 0, 255).astype(np.uint8)
  g = np.clip(g * 1.0, 0, 255).astype(np.uint8)
  b = np.clip(b * 1.8, 0, 255).astype(np.uint8)
  return np.stack([r, g, b], axis=2)


def rgb_to_yuyv(rgb):
  """Convert H×W×3 uint8 RGB to H×2W uint8 YUYV (for v4l2loopback)."""
  r = rgb[:, :, 0].astype(np.float32)
  g = rgb[:, :, 1].astype(np.float32)
  b = rgb[:, :, 2].astype(np.float32)
  y = np.clip( 0.299*r + 0.587*g + 0.114*b,       0, 255).astype(np.uint8)
  u = np.clip(-0.147*r - 0.289*g + 0.436*b + 128, 0, 255).astype(np.uint8)
  v = np.clip( 0.615*r - 0.515*g - 0.100*b + 128, 0, 255).astype(np.uint8)
  yuyv = np.empty((rgb.shape[0], rgb.shape[1] * 2), dtype=np.uint8)
  yuyv[:, 0::4] = y[:, 0::2]   # Y0
  yuyv[:, 1::4] = u[:, 0::2]   # U
  yuyv[:, 2::4] = y[:, 1::2]   # Y1
  yuyv[:, 3::4] = v[:, 0::2]   # V
  return yuyv


def capture_loop(appsrc, sensor_dev, stop_event, v4l2_fd):
  """Read sensor frames, push to pipewiresink and optionally /dev/video20."""
  v4l2 = subprocess.Popen([
      'v4l2-ctl', '-d', sensor_dev,
      '--set-fmt-video=width=1296,height=734,pixelformat=ip3G',
      '--stream-mmap=4', '--stream-to=-', '--stream-count=0',
  ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

  fd = v4l2.stdout.fileno()
  flags = fcntl.fcntl(fd, fcntl.F_GETFL)
  fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

  buf = b''
  frame_count = 0
  try:
      while not stop_event.is_set():
          ready, _, _ = select.select([v4l2.stdout], [], [], 0.5)
          if not ready:
              continue
          chunk = os.read(fd, 65536)
          if not chunk:
              err = v4l2.stderr.read().decode(errors='replace').strip()
              if err:
                  print(f'v4l2-ctl error: {err}', file=sys.stderr, flush=True)
              break
          buf += chunk
          while len(buf) >= FRAME_IN:
              raw  = unpack_ipu3_raw10(buf[:FRAME_IN])
              buf  = buf[FRAME_IN:]
              rgb  = debayer_grbg_half(raw)
              frame_count += 1
              if frame_count % 30 == 0:
                  print(f'  {frame_count} frames', flush=True)
              # Push to pipewiresink (Cheese / PipeWire-native apps)
              gst_buf = Gst.Buffer.new_wrapped(rgb.tobytes())
              appsrc.emit('push-buffer', gst_buf)
              # Write YUYV to v4l2loopback (Firefox V4L2 direct / portal node 83)
              if v4l2_fd is not None:
                  try:
                      os.write(v4l2_fd, rgb_to_yuyv(rgb).tobytes())
                  except OSError as e:
                      if frame_count <= 3 or frame_count % 300 == 0:
                          print(f'v4l2 write error: {e}', file=sys.stderr, flush=True)
  finally:
      v4l2.terminate()
      v4l2.wait()


def main():
  Gst.init(None)

  try:
      sensor_dev = find_cio2_video_node()
  except (RuntimeError, subprocess.CalledProcessError) as e:
      print(f'Device discovery failed: {e}', file=sys.stderr)
      sys.exit(1)
  print(f'Sensor: {sensor_dev}', flush=True)

  try:
      setup_media_pipeline()
  except subprocess.CalledProcessError as e:
      print(f'media-ctl setup failed: {e}', file=sys.stderr)
      sys.exit(1)

  # Open and hold v4l2loopback at startup to lock YUYV 648×367 format
  # BEFORE any consumer (Firefox, WirePlumber) opens the device.
  # Without this, Firefox negotiates MJPG at a different size and gets no frames.
  v4l2_fd = open_v4l2_loopback()

  caps_str = (f'video/x-raw,format=RGB,'
              f'width={OUT_W},height={OUT_H},'
              f'framerate={FRAMERATE}/1')
  pipeline = Gst.parse_launch(
      f'appsrc name=src is-live=true block=false caps="{caps_str}" ! '
      f'videoconvert ! '
      f'pipewiresink name=pw-sink sync=false'
  )
  appsrc  = pipeline.get_by_name('src')
  pw_sink = pipeline.get_by_name('pw-sink')

  stream_props = Gst.Structure.new_from_string(
      'props,'
      'media.class=(string)Video/Source,'
      'media.role=(string)Camera,'
      'node.name=(string)ov9734-webcam,'
      'node.description=(string)"Surface Laptop 2 Webcam"'
  )
  pw_sink.set_property('stream-properties', stream_props)

  lock               = threading.Lock()
  capture_thread     = [None]
  stop_event         = [None]
  last_had_consumers = [0.0]

  def start_capture():
      with lock:
          if capture_thread[0] and capture_thread[0].is_alive():
              return
          print('Consumer connected — starting sensor capture', flush=True)
          stop_event[0] = threading.Event()
          t = threading.Thread(
              target=capture_loop,
              args=(appsrc, sensor_dev, stop_event[0], v4l2_fd),
              daemon=True,
          )
          capture_thread[0] = t
          t.start()

  def stop_capture():
      with lock:
          ev = stop_event[0]
          t  = capture_thread[0]
      if ev:
          ev.set()
      if t:
          t.join(timeout=3)
      with lock:
          capture_thread[0] = None
          stop_event[0]     = None
      print('No consumers — sensor off', flush=True)

  pipeline.set_state(Gst.State.PLAYING)
  print('Idle — PipeWire Video/Source active, sensor off', flush=True)

  glib_loop = GLib.MainLoop()

  def cleanup(sig=None, frame=None):
      print('\nShutting down...', flush=True)
      with lock:
          ev = stop_event[0]
      if ev:
          ev.set()
      t = capture_thread[0]
      if t:
          t.join(timeout=3)
      pipeline.set_state(Gst.State.NULL)
      if v4l2_fd is not None:
          os.close(v4l2_fd)
      glib_loop.quit()

  signal.signal(signal.SIGTERM, cleanup)
  signal.signal(signal.SIGINT,  cleanup)

  def get_consumer_count(pw_node_ids):
      """PipeWire link count on our nodes + direct V4L2 readers on /dev/video20."""
      pw_count = 0
      try:
          data  = json.loads(subprocess.check_output(
              ['pw-dump'], text=True, stderr=subprocess.DEVNULL, timeout=3,
          ))
          links = [o for o in data if o.get('type') == 'PipeWire:Interface:Link']
          pw_count = sum(
              1 for l in links
              if l.get('info', {}).get('output-node-id') in pw_node_ids
          )
      except Exception:
          pass
      # Also detect Firefox and other apps that open /dev/video20 directly
      v4l2_count = 0
      if v4l2_fd is not None:
          try:
              out = subprocess.check_output(
                  ['fuser', V4L2_DEV], stderr=subprocess.DEVNULL,
                  text=True, timeout=2,
              )
              pids = {int(p) for p in out.split() if p.strip().isdigit()}
              pids.discard(os.getpid())
              v4l2_count = len(pids)
          except Exception:
              pass
      return pw_count + v4l2_count

  def find_node_ids():
      """Return (pw_id, v4l2_node_id): our pipewiresink and the v4l2loopback node."""
      pw_id = v4l2_node_id = None
      deadline = time.time() + 15
      while time.time() < deadline:
          try:
              data = json.loads(subprocess.check_output(
                  ['pw-dump'], text=True, stderr=subprocess.DEVNULL, timeout=3,
              ))
              for obj in data:
                  if obj.get('type') == 'PipeWire:Interface:Node':
                      props = obj.get('info', {}).get('props', {})
                      if props.get('node.name') == 'ov9734-webcam':
                          pw_id = obj['id']
                      if (props.get('device.api') == 'v4l2'
                              and 'video20' in props.get('node.name', '')):
                          v4l2_node_id = obj['id']
          except Exception:
              pass
          if pw_id is not None:
              break
          time.sleep(0.5)
      return pw_id, v4l2_node_id

  def pw_watcher():
      pw_id, v4l2_node_id = find_node_ids()
      if pw_id is None:
          print('Warning: PW node not found — consumer detection disabled',
                file=sys.stderr, flush=True)
          return
      node_ids = {n for n in (pw_id, v4l2_node_id) if n is not None}
      print(f'Watching: pipewiresink={pw_id} v4l2loopback={v4l2_node_id}', flush=True)

      while glib_loop.is_running():
          time.sleep(0.25)
          n   = get_consumer_count(node_ids)
          now = time.time()
          with lock:
              running = capture_thread[0] and capture_thread[0].is_alive()
          if n > 0:
              last_had_consumers[0] = now
              if not running:
                  start_capture()
          else:
              if running and (now - last_had_consumers[0]) > STOP_GRACE:
                  stop_capture()

  threading.Thread(target=pw_watcher, daemon=True).start()
  glib_loop.run()


if __name__ == '__main__':
  main()
