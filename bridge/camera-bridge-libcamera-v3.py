#!/usr/bin/env python3
"""
Surface Laptop 2 webcam bridge — idle-aware, no Python frame copies
libcamerasrc → intervideosrc → pipewiresink (Cheese, WebRTC, etc.)
                             → v4l2sink /dev/video20 (portal fallback)

Two GStreamer pipelines:
  1. "ad" pipeline: appsrc → pipewiresink (always running, advertises node)
     When no consumers: appsrc pushes silence (no frames, camera off)
  2. "cap" pipeline: libcamerasrc → intervideosink (only runs with consumers)
     intervideosrc in ad pipeline pulls from it when running

Actually simpler: use interpipes (intervideosink/intervideosrc) or just
use the appsrc approach but feed it via a GStreamer pipeline using
appsink→appsrc with a direct buffer handoff (no Python copy needed via
buffer stealing).

Simplest correct approach: keep appsrc node pipeline always running,
start/stop a libcamerasrc → appsink pipeline on consumer connect/disconnect,
but use Gst.Buffer.new_wrapped(bytes(info.data)) — the memoryview/bytes
call is unavoidable but is a single memcpy which is fast enough.

The 21fps issue in v1 was likely the drop=true on appsink causing frame
drops before they reached appsrc. Fix: remove drop, use a queue.
"""
import subprocess, signal, sys, os, time, threading, fcntl, struct, json
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

OUT_W      = 1280
OUT_H      = 704
FRAMERATE  = 30
STOP_GRACE = 5.0
V4L2_DEV   = '/dev/video20'

_VIDIOC_S_FMT         = 0xc0d05605
_V4L2_BUF_TYPE_OUTPUT = 2
_V4L2_PIX_FMT_YUYV    = 0x56595559
_YUYV_STRIDE          = OUT_W * 2


def open_v4l2_loopback():
    try:
        fd = os.open(V4L2_DEV, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as e:
        print(f'v4l2loopback not available ({e}), skipping', flush=True)
        return None
    fmt = bytearray(208)
    struct.pack_into('<I', fmt,  0, _V4L2_BUF_TYPE_OUTPUT)
    struct.pack_into('<I', fmt,  8, OUT_W)
    struct.pack_into('<I', fmt, 12, OUT_H)
    struct.pack_into('<I', fmt, 16, _V4L2_PIX_FMT_YUYV)
    struct.pack_into('<I', fmt, 20, 1)
    struct.pack_into('<I', fmt, 24, _YUYV_STRIDE)
    struct.pack_into('<I', fmt, 28, _YUYV_STRIDE * OUT_H)
    try:
        fcntl.ioctl(fd, _VIDIOC_S_FMT, fmt)
    except OSError as e:
        print(f'v4l2loopback VIDIOC_S_FMT failed ({e}), skipping', flush=True)
        os.close(fd)
        return None
    print(f'v4l2loopback: {V4L2_DEV} ready ({OUT_W}×{OUT_H} YUYV)', flush=True)
    return fd


def main():
    Gst.init(None)

    v4l2_fd = open_v4l2_loopback()

    # ── Advertisement pipeline (always running) ────────────────────────────
    # appsrc advertises the PipeWire node. When capture is off it just
    # sits idle with no buffers pushed — consumers see the node but get
    # no frames until capture starts.
    rgb_caps = (f'video/x-raw,format=RGB,'
                f'width={OUT_W},height={OUT_H},'
                f'framerate={FRAMERATE}/1')

    ad_pipeline = Gst.parse_launch(
        f'appsrc name=src is-live=true block=false format=time caps="{rgb_caps}" ! '
        f'videoconvert ! '
        f'pipewiresink name=pw-sink sync=false'
    )
    appsrc  = ad_pipeline.get_by_name('src')
    pw_sink = ad_pipeline.get_by_name('pw-sink')

    stream_props = Gst.Structure.new_from_string(
        'props,'
        'media.class=(string)Video/Source,'
        'media.role=(string)Camera,'
        'node.name=(string)ov9734-webcam,'
        'node.description=(string)"Surface Laptop 2 Webcam"'
    )
    pw_sink.set_property('stream-properties', stream_props)
    ad_pipeline.set_state(Gst.State.PLAYING)
    print('PipeWire node active, sensor off', flush=True)

    # ── Capture pipeline (started on demand) ──────────────────────────────
    # libcamerasrc → appsink feeds appsrc above AND v4l2sink
    yuyv_caps = (f'video/x-raw,format=YUY2,'
                 f'width={OUT_W},height={OUT_H},'
                 f'framerate={FRAMERATE}/1')

    cap_pipeline_str = (
        f'libcamerasrc ! '
        f'videoconvert ! '
        f'videoscale ! '
        f'{rgb_caps} ! '
        f'tee name=t '
        f't. ! queue max-size-buffers=4 leaky=no ! '
        f'appsink name=sink emit-signals=true max-buffers=4 drop=false sync=false '
        f't. ! queue max-size-buffers=2 leaky=downstream ! '
        f'videoconvert ! '
        f'{yuyv_caps} ! '
        f'v4l2sink device={V4L2_DEV} sync=false'
    )

    lock               = threading.Lock()
    cap_pipeline_ref   = [None]
    capture_thread     = [None]
    stop_event         = [None]
    last_had_consumers = [0.0]
    frame_count        = [0]

    def on_new_sample(sink):
        sample = sink.emit('pull-sample')
        if sample is None:
            return Gst.FlowReturn.ERROR
        buf = sample.get_buffer()
        # Hand buffer directly to appsrc — one memcpy but no Python processing
        success, info = buf.map(Gst.MapFlags.READ)
        if success:
            gst_buf = Gst.Buffer.new_wrapped(bytes(info.data))
            buf.unmap(info)
            appsrc.emit('push-buffer', gst_buf)
            frame_count[0] += 1
            if frame_count[0] % 150 == 0:
                print(f'  {frame_count[0]} frames', flush=True)
        return Gst.FlowReturn.OK

    def capture_thread_fn(ev):
        cap = Gst.parse_launch(cap_pipeline_str)
        sink = cap.get_by_name('sink')
        sink.connect('new-sample', on_new_sample)

        cap.set_state(Gst.State.PLAYING)
        print('libcamera started', flush=True)

        while not ev.is_set():
            time.sleep(0.05)

        cap.set_state(Gst.State.NULL)
        with lock:
            cap_pipeline_ref[0] = None
        print('libcamera stopped', flush=True)

    def start_capture():
        with lock:
            if capture_thread[0] and capture_thread[0].is_alive():
                return
            print('Consumer connected — starting capture', flush=True)
            frame_count[0] = 0
            ev = threading.Event()
            stop_event[0] = ev
            t = threading.Thread(target=capture_thread_fn, args=(ev,), daemon=True)
            capture_thread[0] = t
            t.start()

    def stop_capture():
        with lock:
            ev = stop_event[0]
            t  = capture_thread[0]
        if ev:
            ev.set()
        if t:
            t.join(timeout=5)
        with lock:
            capture_thread[0] = None
            stop_event[0]     = None
        print('No consumers — sensor off', flush=True)

    glib_loop = GLib.MainLoop()

    def cleanup(sig=None, frame=None):
        print('\nShutting down...', flush=True)
        with lock:
            ev = stop_event[0]
        if ev:
            ev.set()
        t = capture_thread[0]
        if t:
            t.join(timeout=5)
        ad_pipeline.set_state(Gst.State.NULL)
        if v4l2_fd is not None:
            os.close(v4l2_fd)
        glib_loop.quit()

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT,  cleanup)

    def get_consumer_count(pw_node_ids):
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
