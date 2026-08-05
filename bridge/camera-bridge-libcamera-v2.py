#!/usr/bin/env python3
"""
Surface Laptop 2 webcam bridge — single GStreamer pipeline
libcamerasrc → tee → pipewiresink (Cheese, WebRTC, etc.)
                   → v4l2sink /dev/video20 (portal fallback)

No Python frame copies. libcamera provides auto-exposure and AWB.
The pipeline runs continuously while the service is active.
"""
import subprocess, signal, sys, os, time, fcntl, struct, json
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

OUT_W      = 1280
OUT_H      = 704
FRAMERATE  = 30
V4L2_DEV   = '/dev/video20'

# V4L2 constants
_VIDIOC_S_FMT         = 0xc0d05605
_V4L2_BUF_TYPE_OUTPUT = 2
_V4L2_PIX_FMT_YUYV    = 0x56595559
_YUYV_STRIDE          = OUT_W * 2


def open_v4l2_loopback():
    """Open /dev/video20 and lock YUYV format before any consumer opens it."""
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
    struct.pack_into('<I', fmt, 20, 1)  # V4L2_FIELD_NONE
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

    # Lock v4l2loopback format before any consumer opens the device
    v4l2_fd = open_v4l2_loopback()
    if v4l2_fd is not None:
        os.close(v4l2_fd)  # just needed the ioctl to set format; GStreamer opens it

    raw_caps  = (f'video/x-raw,format=RGB,'
                 f'width={OUT_W},height={OUT_H},'
                 f'framerate={FRAMERATE}/1')
    yuyv_caps = (f'video/x-raw,format=YUY2,'
                 f'width={OUT_W},height={OUT_H},'
                 f'framerate={FRAMERATE}/1')

    pipeline_str = (
        f'libcamerasrc ! '
        f'videoconvert ! '
        f'videoscale ! '
        f'{raw_caps} ! '
        f'tee name=t '

        # Branch 1: PipeWire (Cheese, WebRTC, portal)
        f't. ! queue max-size-buffers=2 leaky=downstream ! '
        f'pipewiresink name=pw-sink sync=false '

        # Branch 2: v4l2loopback (portal fallback, direct V4L2 readers)
        f't. ! queue max-size-buffers=2 leaky=downstream ! '
        f'videoconvert ! '
        f'{yuyv_caps} ! '
        f'v4l2sink device={V4L2_DEV} sync=false'
    )

    pipeline = Gst.parse_launch(pipeline_str)
    pw_sink  = pipeline.get_by_name('pw-sink')

    stream_props = Gst.Structure.new_from_string(
        'props,'
        'media.class=(string)Video/Source,'
        'media.role=(string)Camera,'
        'node.name=(string)ov9734-webcam,'
        'node.description=(string)"Surface Laptop 2 Webcam"'
    )
    pw_sink.set_property('stream-properties', stream_props)

    glib_loop = GLib.MainLoop()

    def on_message(bus, message):
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            print(f'GStreamer error: {err} ({dbg})', file=sys.stderr, flush=True)
            glib_loop.quit()
        elif t == Gst.MessageType.EOS:
            print('EOS received', flush=True)
            glib_loop.quit()

    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect('message', on_message)

    def cleanup(sig=None, frame=None):
        print('\nShutting down...', flush=True)
        pipeline.set_state(Gst.State.NULL)
        glib_loop.quit()

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT,  cleanup)

    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        print('Failed to start pipeline', file=sys.stderr)
        sys.exit(1)

    print(f'Pipeline running: libcamera → PipeWire + {V4L2_DEV}', flush=True)
    glib_loop.run()
    pipeline.set_state(Gst.State.NULL)


if __name__ == '__main__':
    main()
