#!/usr/bin/env python3
"""
Surface Laptop 2 webcam bridge — PipeWire + V4L2 loopback, idle-aware
libcamera (auto-exposure, AWB) → pipewiresink (node for Cheese etc.)
                               → /dev/video20 YUYV (for Firefox V4L2)
"""
import subprocess, signal, sys, os, time, threading, fcntl, select, json, struct
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

OUT_W      = 1280
OUT_H      = 704
FRAMERATE  = 30
STOP_GRACE = 5.0
V4L2_DEV   = '/dev/video20'

# V4L2 constants
_VIDIOC_S_FMT              = 0xc0d05605
_V4L2_BUF_TYPE_OUTPUT      = 2
_V4L2_PIX_FMT_YUYV         = 0x56595559  # 'YUYV' little-endian
_YUYV_STRIDE               = OUT_W * 2


def open_v4l2_loopback():
    """Open /dev/video20 as writer and fix YUYV format via ioctl to lock it."""
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


def capture_loop(appsrc, stop_event):
    """
    Capture native NV12 frames from libcamera, and push the unmapped buffer
    objects directly into appsrc. GStreamer handles the copying and splitting.
    """
    camera_caps = f'video/x-raw,format=NV12,width={OUT_W},height={OUT_H},framerate={FRAMERATE}/1'
    
    pipeline_str = (
        f'libcamerasrc ! '
        f'{camera_caps} ! '
        f'appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false'
    )
    cap_pipeline = Gst.parse_launch(pipeline_str)
    appsink = cap_pipeline.get_by_name('sink')

    frame_count = 0

    def on_new_sample(sink):
        nonlocal frame_count
        sample = sink.emit('pull-sample')
        if sample is None:
            return Gst.FlowReturn.ERROR

        buf = sample.get_buffer()
        frame_count += 1
        if frame_count % 30 == 0:
            print(f'  {frame_count} hardware frames processed', flush=True)

        appsrc.emit('push-buffer', buf)
        return Gst.FlowReturn.OK

    appsink.connect('new-sample', on_new_sample)

    cap_pipeline.set_state(Gst.State.PLAYING)
    print('libcamera lightweight capture loop started', flush=True)

    while not stop_event.is_set():
        time.sleep(0.1)

    cap_pipeline.set_state(Gst.State.NULL)
    print('libcamera pipeline stopped', flush=True)


def main():
    Gst.init(None)

    v4l2_fd = open_v4l2_loopback()

    caps_str = (
        f'video/x-raw, width={OUT_W}, height={OUT_H}, framerate={FRAMERATE}/1, '
        f'format=(string){{RGB, YUY2, I420, NV12}}'
    )
    
    pipeline_str = (
        f'appsrc name=src is-live=true block=false caps="{caps_str}" ! videoconvert ! tee name=t '
        f't. ! queue max-size-buffers=2 leaky=downstream ! pipewiresink name=pw-sink sync=false '
        f't. ! queue max-size-buffers=2 leaky=downstream ! videoconvert ! video/x-raw,format=YUY2 ! v4l2sink device={V4L2_DEV} sync=false'
    )
    
    pipeline = Gst.parse_launch(pipeline_str)
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
    last_had_consumers = [time.time()]

    def start_capture():
        with lock:
            if capture_thread[0] and capture_thread[0].is_alive():
                return
            print('Consumer connected — starting libcamera capture', flush=True)
            stop_event[0] = threading.Event()
            t = threading.Thread(
                target=capture_loop,
                args=(appsrc, stop_event[0]),
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
        print('\nShutting down cleanly...', flush=True)
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

    # Cache our own PID so we can ignore it during fuser scans
    my_pid = str(os.getpid())

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
                    ['fuser', V4L2_DEV], stderr=subprocess.DEVNULL, text=True, timeout=2
                )
                # Filter out our own PID from the fuser output list
                pids = [p for p in out.strip().split() if p != my_pid]
                v4l2_count = len(pids)
            except Exception:
                pass
        return pw_count + v4l2_count

    pw_node_ids = set()

    # This function replaces the rough infinite loop. 
    # Running inside GLib prevents double Ctrl+C issues.
    def monitor_tick():
        nonlocal pw_node_ids, last_had_consumers
        
        if not pw_node_ids:
            try:
                data = json.loads(subprocess.check_output(['pw-dump'], text=True, timeout=2))
                pw_node_ids = {o['id'] for o in data if o.get('info', {}).get('node.name') == 'ov9734-webcam'}
            except Exception:
                pass

        count = get_consumer_count(pw_node_ids)
        has_consumers = (count > 0)

        if has_consumers:
            last_had_consumers = time.time()
            if not capture_thread[0]:
                start_capture()
        else:
            if capture_thread[0] and (time.time() - last_had_consumers > STOP_GRACE):
                stop_capture()
                
        return True  # Tells GLib to keep running this timer tick

    # Run the monitor loop once every 1000ms (1 second) natively inside GLib
    GLib.timeout_add(1000, monitor_tick)

    glib_loop.run()


if __name__ == '__main__':
    main()
