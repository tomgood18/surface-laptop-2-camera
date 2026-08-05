#!/usr/bin/env python3
"""
Surface Laptop 2 webcam bridge — PipeWire + V4L2 loopback, idle-aware
libcamera (auto-exposure, AWB) → pipewiresink (node for Cheese etc.)
                               → /dev/video20 YUYV (for Firefox V4L2
                                 direct access and portal)

Replaces raw IPU3 RAW10 capture + Python debayer with a GStreamer
libcamerasrc pipeline. Everything else (PipeWire node, consumer
detection, idle management, v4l2loopback) is unchanged.
"""
import subprocess, signal, sys, os, time, threading, fcntl, select, json, struct
import numpy as np
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

SENSOR_W   = 1296
SENSOR_H   = 734
#OUT_W      = SENSOR_W // 2
#OUT_H      = SENSOR_H // 2
OUT_W      = 1296
OUT_H      = 734
FRAMERATE  = 30
STOP_GRACE = 5.0
V4L2_DEV   = '/dev/video20'

# V4L2 constants
_VIDIOC_S_FMT              = 0xc0d05605
_V4L2_BUF_TYPE_OUTPUT      = 2
_V4L2_PIX_FMT_YUYV         = 0x56595559  # 'YUYV' little-endian
_YUYV_STRIDE               = OUT_W * 2


def open_v4l2_loopback():
    """Open /dev/video20 as writer and fix YUYV format via ioctl."""
    try:
        fd = os.open(V4L2_DEV, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as e:
        print(f'v4l2loopback not available ({e}), skipping', flush=True)
        return None

    def _sfmt(buf_type):
        fmt = bytearray(208)
        struct.pack_into('<I', fmt,  0, buf_type)
        struct.pack_into('<I', fmt,  8, OUT_W)
        struct.pack_into('<I', fmt, 12, OUT_H)
        struct.pack_into('<I', fmt, 16, _V4L2_PIX_FMT_YUYV)
        struct.pack_into('<I', fmt, 20, 1)  # V4L2_FIELD_NONE
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


def rgb_to_yuyv(rgb):
    """Convert H×W×3 uint8 RGB to H×2W uint8 YUYV (for v4l2loopback)."""
    r = rgb[:, :, 0].astype(np.float32)
    g = rgb[:, :, 1].astype(np.float32)
    b = rgb[:, :, 2].astype(np.float32)
    y = np.clip( 0.299*r + 0.587*g + 0.114*b,       0, 255).astype(np.uint8)
    u = np.clip(-0.147*r - 0.289*g + 0.436*b + 128, 0, 255).astype(np.uint8)
    v = np.clip( 0.615*r - 0.515*g - 0.100*b + 128, 0, 255).astype(np.uint8)
    yuyv = np.empty((rgb.shape[0], rgb.shape[1] * 2), dtype=np.uint8)
    yuyv[:, 0::4] = y[:, 0::2]
    yuyv[:, 1::4] = u[:, 0::2]
    yuyv[:, 2::4] = y[:, 1::2]
    yuyv[:, 3::4] = v[:, 0::2]
    return yuyv



def capture_loop(appsrc, stop_event, v4l2_fd):
    """
    Capture via libcamera using GStreamer libcamerasrc, push YUY2 frames
    with proper timestamps to pipewiresink appsrc and to /dev/video20.
    """
    # Keep your exact, proven camera caps and pipeline string
    #caps = f'video/x-raw,format=YUY2,width={OUT_W},height={OUT_H},framerate={FRAMERATE}/1'
    #pipeline_str = (
    #    f'libcamerasrc ! '
    #    f'videoconvert ! '
    #    f'videoscale ! '
    #    f'{caps} ! '
    #    f'appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false'
    #)
    
    # Force libcamera to skip unnecessary color conversions and stay lightweight
    pipeline_str = (
        f'libcamerasrc ! '
        f'video/x-raw,format=NV12 ! '  # Native format for the Soft ISP, avoids internal RGB/YUY2 conversion overhead
        f'videoscale ! '
        f'videoconvert ! '
        f'video/x-raw,format=YUY2,width={OUT_W},height={OUT_H},framerate={FRAMERATE}/1 ! '
        f'appsink name=sink emit-signals=true max-buffers=4 drop=false sync=false'
    )
    cap_pipeline = Gst.parse_launch(pipeline_str)
    appsink = cap_pipeline.get_by_name('sink')

    frame_count = [0]
    
    # Track timestamps manually in nanoseconds
    # 30 FPS means each frame lasts ~33,333,333 nanoseconds
    frame_duration = int(1_000_000_000 / FRAMERATE)
    pts_counter = [0] 

    def on_new_sample(sink):
        sample = sink.emit('pull-sample')
        if sample is None:
            return Gst.FlowReturn.ERROR

        buf = sample.get_buffer()
        success, info = buf.map(Gst.MapFlags.READ)
        if not success:
            return Gst.FlowReturn.ERROR

        try:
            data = bytes(info.data)
            frame_count[0] += 1
            if frame_count[0] % 30 == 0:
                print(f'  {frame_count[0]} frames', flush=True)

            # Wrap the raw data bytes
            gst_buf = Gst.Buffer.new_wrapped(data)
            
            # Inject strict presentation timestamps so Firefox and Cheese stay in sync
            gst_buf.pts = buf.pts
            gst_buf.dts = buf.dts
            gst_buf.duration = buf.duration
            #gst_buf.pts = pts_counter[0]
            #gst_buf.dts = pts_counter[0]
            #gst_buf.duration = frame_duration
            
            # Step forward in time for the next frame
            #pts_counter[0] += frame_duration

            # Push to pipewiresink
            appsrc.emit('push-buffer', gst_buf)

            # Write YUY2 directly to v4l2loopback
            if v4l2_fd is not None:
                try:
                    os.write(v4l2_fd, data)
                except OSError as e:
                    if frame_count[0] <= 3 or frame_count[0] % 300 == 0:
                        print(f'v4l2 write error: {e}', file=sys.stderr, flush=True)
        finally:
            buf.unmap(info)

        return Gst.FlowReturn.OK

    appsink.connect('new-sample', on_new_sample)

    cap_pipeline.set_state(Gst.State.PLAYING)
    print('libcamera pipeline started with manual PTS injection', flush=True)

    while not stop_event.is_set():
        time.sleep(0.1)

    cap_pipeline.set_state(Gst.State.NULL)
    print('libcamera pipeline stopped', flush=True)


#    def on_new_sample(sink):
#        sample = sink.emit('pull-sample')
#        if sample is None:
#            return Gst.FlowReturn.ERROR
#
#        buf = sample.get_buffer()
#        success, info = buf.map(Gst.MapFlags.READ)
#        if not success:
#            return Gst.FlowReturn.ERROR
#
#        try:
#            raw_bytes = bytes(info.data)
#            frame_count[0] += 1
#            if frame_count[0] % 30 == 0:
#                print(f'  {frame_count[0]} frames', flush=True)
#
#            # 1. Push raw YUY2 bytes straight into your main PipeWire appsrc
#            gst_buf = Gst.Buffer.new_wrapped(raw_bytes)
#            appsrc.emit('push-buffer', gst_buf)
#
#            # 2. Write the exact same raw YUY2 bytes straight into /dev/video20 
#            if v4l2_fd is not None:
#                try:
#                    os.write(v4l2_fd, raw_bytes)
#                except OSError as e:
#                    if frame_count[0] % 300 == 0:
#                        print(f'v4l2 write error: {e}', file=sys.stderr, flush=True)
#        finally:
#            buf.unmap(info)
#
#        return Gst.FlowReturn.OK

    appsink.connect('new-sample', on_new_sample)
    cap_pipeline.set_state(Gst.State.PLAYING)
    print('Pure-YUY2 libcamera pipeline started', flush=True)

    while not stop_event.is_set():
        time.sleep(0.1)

    cap_pipeline.set_state(Gst.State.NULL)
    print('libcamera pipeline stopped', flush=True)

#def capture_loop(appsrc, stop_event, v4l2_fd):
#    """
#    Capture via libcamera using GStreamer libcamerasrc, push RGB frames
#    to pipewiresink appsrc and optionally to /dev/video20 as YUYV.
#    """
#    # Build a GStreamer pipeline:
#    #   libcamerasrc → scale to OUT_W×OUT_H → RGB → appsink
#    caps = (f'video/x-raw,format=YUY2,width={OUT_W},height={OUT_H},'
#            f'framerate={FRAMERATE}/1')
#    pipeline_str = (
#        f'libcamerasrc ! '
#        f'videoconvert ! '
#        f'videoscale ! '
#        f'{caps} ! '
#        f'appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false'
#    )
#    cap_pipeline = Gst.parse_launch(pipeline_str)
#    appsink = cap_pipeline.get_by_name('sink')
#
#    frame_count = [0]
#
#    def on_new_sample(sink):
#        sample = sink.emit('pull-sample')
#        if sample is None:
#            return Gst.FlowReturn.ERROR
#
#        buf = sample.get_buffer()
#        success, info = buf.map(Gst.MapFlags.READ)
#        if not success:
#            return Gst.FlowReturn.ERROR
#
#        try:
#            data = bytes(info.data)
#            frame_count[0] += 1
#            if frame_count[0] % 30 == 0:
#                print(f'  {frame_count[0]} frames', flush=True)
#
#            # Push to pipewiresink (Cheese / PipeWire-native apps)
#            gst_buf = Gst.Buffer.new_wrapped(data)
#            appsrc.emit('push-buffer', gst_buf)
#
#            # Write YUYV to v4l2loopback (Firefox V4L2 direct / portal)
#            if v4l2_fd is not None:
#                rgb = np.frombuffer(data, dtype=np.uint8).reshape(OUT_H, OUT_W, 3)
#                try:
#                    os.write(v4l2_fd, rgb_to_yuyv(rgb).tobytes())
#                except OSError as e:
#                    if frame_count[0] <= 3 or frame_count[0] % 300 == 0:
#                        print(f'v4l2 write error: {e}', file=sys.stderr, flush=True)
#        finally:
#            buf.unmap(info)
#
#        return Gst.FlowReturn.OK
#
#    appsink.connect('new-sample', on_new_sample)
#
#    cap_pipeline.set_state(Gst.State.PLAYING)
#    print('libcamera pipeline started', flush=True)
#
#    # Wait until stop_event is set
#    while not stop_event.is_set():
#        time.sleep(0.1)
#
#    cap_pipeline.set_state(Gst.State.NULL)
#    print('libcamera pipeline stopped', flush=True)


def main():
    Gst.init(None)

    # Open and hold v4l2loopback at startup to lock YUYV format
    v4l2_fd = open_v4l2_loopback()

    caps_str = (f'video/x-raw,format=YUY2,'
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
            print('Consumer connected — starting libcamera capture', flush=True)
            stop_event[0] = threading.Event()
            t = threading.Thread(
                target=capture_loop,
                args=(appsrc, stop_event[0], v4l2_fd),
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
            t.join(timeout=5)
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
            t.join(timeout=5)
        pipeline.set_state(Gst.State.NULL)
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
