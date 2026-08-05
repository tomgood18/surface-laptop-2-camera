#!/usr/bin/env python3
"""
Surface Laptop 2 webcam bridge — zero-copy, idle-aware
libcamerasrc → intervideosink ─┐
                               ├→ intervideosrc → pipewiresink  (always-on PW node)
                libcamerasrc → v4l2sink /dev/video20

Two GStreamer pipelines share frames via intervideo (zero copy):
- Announce pipeline: intervideosrc → pipewiresink  (always running)
- Capture pipeline:  libcamerasrc → tee → intervideosink + v4l2sink
                     (starts/stops with consumers)

The PipeWire node is always present so apps can discover the camera.
The sensor only runs when a consumer connects.
"""
import subprocess, signal, sys, os, time, fcntl, struct, json, threading
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

SENSOR_W   = 1280
SENSOR_H   = 720
#OUT_W      = SENSOR_W // 2
#OUT_H      = SENSOR_H // 2
OUT_W      = SENSOR_W
OUT_H      = SENSOR_H
FRAMERATE  = 30
STOP_GRACE = 5.0
V4L2_DEV   = '/dev/video20'
INTERVIDEO_CHANNEL = 'ov9734-channel'

# V4L2 constants
_VIDIOC_S_FMT         = 0xc0d05605
_V4L2_BUF_TYPE_OUTPUT = 2
_V4L2_PIX_FMT_YUYV    = 0x56595559
_YUYV_STRIDE          = OUT_W * 2


def open_v4l2_loopback():
    """Lock v4l2loopback format before any consumer opens the device."""
    try:
        fd = os.open(V4L2_DEV, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as e:
        print(f'v4l2loopback not available ({e}), skipping', flush=True)
        return False

    fmt = bytearray(208)
    struct.pack_into('<I', fmt,  0, _V4L2_BUF_TYPE_OUTPUT)
    struct.pack_into('<I', fmt,  8, OUT_W)
    struct.pack_into('<I', fmt, 12, OUT_H)
    struct.pack_into('<I', fmt, 16, _V4L2_PIX_FMT_YUYV)
    struct.pack_into('<I', fmt, 20, 1)  # V4L2_FIELD_NONE
    struct.pack_into('<I', fmt, 24, _YUYV_STRIDE)
    struct.pack_into('<I', fmt, 28, _YUYV_STRIDE * OUT_H)

    try:
        fcntl.ioctl(fd, _VIDIOC_S_FMT, fmt)
        os.close(fd)
    except OSError as e:
        print(f'v4l2loopback VIDIOC_S_FMT failed ({e}), skipping', flush=True)
        os.close(fd)
        return False

    print(f'v4l2loopback: {V4L2_DEV} ready ({OUT_W}×{OUT_H} YUYV)', flush=True)
    return True


def make_announce_pipeline():
    """
    Always-on pipeline: reads from intervideosrc (fed by capture pipeline)
    and pushes to PipeWire. When no capture pipeline is running, intervideosrc
    produces black frames, keeping the PipeWire node alive.
    """
    raw_caps = (f'video/x-raw,format=YUY2,'
                f'width={OUT_W},height={OUT_H},'
                f'framerate={FRAMERATE}/1')
    pipeline = Gst.parse_launch(
        f'intervideosrc channel={INTERVIDEO_CHANNEL} ! '
        f'{raw_caps} ! '
        f'pipewiresink name=pw-sink sync=true'
    )
    pw_sink = pipeline.get_by_name('pw-sink')
    stream_props = Gst.Structure.new_from_string(
        'props,'
        'media.class=(string)Video/Source,'
        'media.role=(string)Camera,'
        'node.name=(string)ov9734-webcam,'
        'node.description=(string)"Surface Laptop 2 Webcam"'
    )
    pw_sink.set_property('stream-properties', stream_props)
    return pipeline


def make_capture_pipeline(has_v4l2):
    """
    On-demand pipeline: libcamerasrc → tee → intervideosink (feeds announce)
                                           → v4l2sink (feeds loopback)
    """
    yuyv_caps = (f'video/x-raw,format=YUY2,'
                 f'width={OUT_W},height={OUT_H},'
                 f'framerate={FRAMERATE}/1')

    if has_v4l2:
        pipeline_str = (
            f'libcamerasrc ! '
            f'videoconvert ! videoscale ! {yuyv_caps} ! '
            f'tee name=t '
            f't. ! queue max-size-buffers=2 leaky=downstream ! '
            f'intervideosink channel={INTERVIDEO_CHANNEL} '
            f't. ! queue max-size-buffers=2 leaky=downstream ! '
            f'v4l2sink device={V4L2_DEV} sync=false'
        )
    else:
        pipeline_str = (
            f'libcamerasrc ! '
            f'videoconvert ! videoscale ! {raw_caps} ! '
            f'intervideosink channel={INTERVIDEO_CHANNEL}'
        )

    return Gst.parse_launch(pipeline_str)


def main():
    Gst.init(None)

    has_v4l2 = open_v4l2_loopback()

    glib_loop = GLib.MainLoop()
    lock      = threading.Lock()
    state     = {'active': False, 'cap_pipeline': None}
    last_consumers = [0.0]

    # Start the announce pipeline — always on
    announce = make_announce_pipeline()
    announce.set_state(Gst.State.PLAYING)
    print('Idle — PipeWire node active, sensor off', flush=True)

    def start_capture():
        with lock:
            if state['active']:
                return
            print('Consumer connected — starting libcamera', flush=True)
            cap = make_capture_pipeline(has_v4l2)

            def on_message(bus, message):
                if message.type == Gst.MessageType.ERROR:
                    err, dbg = message.parse_error()
                    print(f'Capture error: {err}', file=sys.stderr, flush=True)
                    stop_capture()

            cap.get_bus().add_signal_watch()
            cap.get_bus().connect('message', on_message)
            cap.set_state(Gst.State.PLAYING)
            state['cap_pipeline'] = cap
            state['active'] = True

    def stop_capture():
        with lock:
            if not state['active']:
                return
            print('No consumers — sensor off', flush=True)
            if state['cap_pipeline']:
                state['cap_pipeline'].set_state(Gst.State.NULL)
                state['cap_pipeline'] = None
            state['active'] = False

    def cleanup(sig=None, frame=None):
        print('\nShutting down...', flush=True)
        stop_capture()
        announce.set_state(Gst.State.NULL)
        glib_loop.quit()

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT,  cleanup)

    def get_consumer_count(node_ids):
        pw_count = 0
        try:
            data = json.loads(subprocess.check_output(
                ['pw-dump'], text=True, stderr=subprocess.DEVNULL, timeout=3,
            ))
            links = [o for o in data if o.get('type') == 'PipeWire:Interface:Link']
            pw_count = sum(
                1 for l in links
                if l.get('info', {}).get('output-node-id') in node_ids
            )
        except Exception:
            pass
        if has_v4l2:
            try:
                out = subprocess.check_output(
                    ['fuser', V4L2_DEV], stderr=subprocess.DEVNULL,
                    text=True, timeout=2,
                )
                pids = {int(p) for p in out.split() if p.strip().isdigit()}
                pids.discard(os.getpid())
                pw_count += len(pids)
            except Exception:
                pass
        return pw_count

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
        print(f'Watching: pipewiresink={pw_id} v4l2loopback={v4l2_node_id}',
              flush=True)

        while glib_loop.is_running():
            time.sleep(0.25)
            n   = get_consumer_count(node_ids)
            now = time.time()
            with lock:
                active = state['active']
            if n > 0:
                last_consumers[0] = now
                if not active:
                    start_capture()
            else:
                if active and (now - last_consumers[0]) > STOP_GRACE:
                    stop_capture()

    threading.Thread(target=pw_watcher, daemon=True).start()
    glib_loop.run()


if __name__ == '__main__':
    main()
