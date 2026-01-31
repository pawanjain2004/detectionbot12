import json
import threading
import time
import urllib.request

import cv2
import numpy as np
import board
import busio
import adafruit_mlx90640
from websocket import create_connection, WebSocketTimeoutException

try:
    import RPi.GPIO as GPIO
except Exception:
    GPIO = None

SERVER_BASE = "wss://legionm3.onrender.com"
SERVER_HTTP = "https://legionm3.onrender.com"
ROBOT_UUID = "robot-002"
ROBOT_TYPE = "rpi-rover"

TARGET_FPS = 8
FRAME_WIDTH = 640
FRAME_HEIGHT = 360
JPEG_QUALITY = 60
MAX_FRAME_DROP = 5

USB_CAM_INDEX = 0
USB_CAM_CANDIDATES = [USB_CAM_INDEX, 1, 2, 3]
THERMAL_FPS = 8
THERMAL_FRAME_WIDTH = 320
THERMAL_FRAME_HEIGHT = 240
THERMAL_JPEG_QUALITY = 70
TEMP_MIN = 20.0
TEMP_MAX = 40.0

VIDEO_URL = f"{SERVER_BASE}/ws/video/robot/{ROBOT_UUID}"
THERMAL_URL = f"{SERVER_BASE}/ws/thermal/robot/{ROBOT_UUID}"
COMMAND_URL = f"{SERVER_BASE}/ws/command/robot/{ROBOT_UUID}"
TELEMETRY_URL = f"{SERVER_BASE}/ws/telemetry/robot/{ROBOT_UUID}"

MOTOR_PINS = {
    "in1": 17,
    "in2": 27,
    "in3": 23,
    "in4": 24,
    "ena": 18,
    "enb": 25,
}
MOTOR_SPEED = 70
DEFAULT_SPEED = 170
LAST_SPEED = DEFAULT_SPEED
_drive_forward = 0
_drive_turn = 0

_mlx_lock = threading.Lock()
_mlx = None
_usb_frame_lock = threading.Lock()
_usb_frame_store = {"frame": None}


def _connect(url, timeout=10):
    ws = create_connection(url, timeout=timeout)
    return ws


def _register_robot():
    payload = json.dumps({"uuid": ROBOT_UUID, "type": ROBOT_TYPE}).encode("utf-8")
    req = urllib.request.Request(
        f"{SERVER_HTTP}/api/robots/register",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                print(f"Device registered: {ROBOT_UUID}")
            return resp.status == 200
    except Exception as e:
        print(f"Robot register error: {e}")
        return False


def _command_listener():
    ws = None
    while True:
        if ws is None:
            try:
                ws = _connect(COMMAND_URL, timeout=10)
                ws.settimeout(5)
                print("Command socket connected")
            except Exception as e:
                print(f"Command socket error: {e}")
                time.sleep(2)
                continue
        try:
            msg = ws.recv()
            if msg is None:
                raise RuntimeError("Command socket closed")
            print(f"[COMMAND] {msg}")
            _handle_command(msg)
        except WebSocketTimeoutException:
            try:
                ws.ping()
            except Exception:
                try:
                    ws.close()
                except Exception:
                    pass
                ws = None
                time.sleep(1)
        except Exception as e:
            print(f"Command recv error: {e}")
            try:
                ws.close()
            except Exception:
                pass
            ws = None
            time.sleep(1)


def _handle_command(msg):
    command = msg
    speed = None
    if isinstance(msg, str):
        try:
            payload = json.loads(msg)
            command = payload.get("command", msg)
            speed = payload.get("speed")
        except Exception:
            command = msg
    if not command:
        return
    if speed is not None:
        _set_speed(speed)
    if command == "MOVE_FORWARD":
        _set_drive_state(forward=1)
    elif command == "MOVE_BACK":
        _set_drive_state(forward=-1)
    elif command == "MOVE_LEFT":
        _set_drive_state(turn=-1)
    elif command == "MOVE_RIGHT":
        _set_drive_state(turn=1)
    elif command == "MOVE_FORWARD_RIGHT":
        _set_drive_state(forward=1, turn=1)
    elif command == "MOVE_FORWARD_LEFT":
        _set_drive_state(forward=1, turn=-1)
    elif command == "MOVE_BACK_RIGHT":
        _set_drive_state(forward=-1, turn=1)
    elif command == "MOVE_BACK_LEFT":
        _set_drive_state(forward=-1, turn=-1)
    elif command == "FORWARD_STOP":
        _set_drive_state(forward=0)
    elif command == "BACK_STOP":
        _set_drive_state(forward=0)
    elif command == "LEFT_STOP":
        _set_drive_state(turn=0)
    elif command == "RIGHT_STOP":
        _set_drive_state(turn=0)
    elif command == "STOP":
        _set_drive_state(forward=0, turn=0)


def _telemetry_sender():
    ws = None
    while True:
        if ws is None:
            try:
                ws = _connect(TELEMETRY_URL)
                print("Telemetry socket connected")
            except Exception as e:
                print(f"Telemetry socket error: {e}")
                time.sleep(2)
                continue
        max_temp = _get_max_temp()
        payload = {
            "uuid": ROBOT_UUID,
            "gas_ppm": 0,
            "temperature_c": max_temp,
            "ts": int(time.time()),
        }
        try:
            ws.send(json.dumps(payload))
        except Exception as e:
            print(f"Telemetry send error: {e}")
            try:
                ws.close()
            except Exception:
                pass
            ws = None
            time.sleep(1)
        time.sleep(1)


def _open_capture(index, width, height, name, backend):
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    return cap


def _open_capture_with_fallbacks(width, height, name):
    backends = [cv2.CAP_V4L2, cv2.CAP_ANY]
    for index in USB_CAM_CANDIDATES:
        for backend in backends:
            cap = _open_capture(index, width, height, name, backend)
            if cap is None:
                continue
            ok, _ = cap.read()
            if ok:
                print(f"{name} camera opened at index {index}")
                return cap
            cap.release()
    print(f"{name} camera not available on indexes: {USB_CAM_CANDIDATES}")
    return None


def _init_mlx():
    global _mlx
    try:
        i2c = busio.I2C(board.SCL, board.SDA, frequency=400000)
        mlx = adafruit_mlx90640.MLX90640(i2c)
        mlx.refresh_rate = adafruit_mlx90640.RefreshRate.REFRESH_2_HZ
        _mlx = mlx
        print("MLX90640 sensor initialized")
    except Exception as e:
        _mlx = None
        print(f"MLX90640 init error: {e}")


def _get_thermal_frame():
    if _mlx is None:
        return None
    frame = np.zeros(24 * 32, dtype=np.float32)
    try:
        with _mlx_lock:
            _mlx.getFrame(frame)
    except Exception:
        return None
    return frame.reshape((24, 32))


def _thermal_to_image(temp):
    temp = np.clip(temp, TEMP_MIN, TEMP_MAX)
    norm = (temp - TEMP_MIN) / (TEMP_MAX - TEMP_MIN)
    img = (norm * 255).astype(np.uint8)
    img = cv2.applyColorMap(img, cv2.COLORMAP_JET)
    img = cv2.resize(
        img, (THERMAL_FRAME_WIDTH, THERMAL_FRAME_HEIGHT), interpolation=cv2.INTER_CUBIC
    )
    return img


def _get_max_temp():
    temp = _get_thermal_frame()
    if temp is None:
        return None
    return float(np.max(temp))


def _frame_sender(cap, ws_url, stream_name, target_fps, jpeg_quality):
    ws = None
    next_frame_time = time.monotonic()
    while True:
        if cap is None or not cap.isOpened():
            time.sleep(2)
            continue
        if ws is None:
            try:
                ws = _connect(ws_url)
                print(f"{stream_name} socket connected")
            except Exception as e:
                print(f"{stream_name} socket error: {e}")
                time.sleep(2)
                continue
        now = time.monotonic()
        if now < next_frame_time:
            time.sleep(next_frame_time - now)
        else:
            lateness = now - next_frame_time
            if lateness > 0:
                drop_count = min(int(lateness * target_fps), MAX_FRAME_DROP)
                for _ in range(drop_count):
                    cap.grab()
            next_frame_time = now
        if not cap.grab():
            continue
        ret, frame = cap.retrieve()
        if not ret:
            continue
        ok, buffer = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality],
        )
        if not ok:
            continue
        try:
            ws.send(buffer.tobytes(), opcode=0x2)
        except Exception as e:
            print(f"{stream_name} send error: {e}")
            try:
                ws.close()
            except Exception:
                pass
            ws = None
            time.sleep(1)
            continue
        next_frame_time += 1.0 / target_fps


def _capture_loop(cap, frame_store, frame_lock):
    while True:
        if cap is None or not cap.isOpened():
            time.sleep(0.5)
            continue
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.01)
            continue
        with frame_lock:
            frame_store["frame"] = frame


def _frame_sender_latest(frame_store, frame_lock, ws_url, stream_name, target_fps, jpeg_quality):
    ws = None
    next_frame_time = time.monotonic()
    while True:
        if ws is None:
            try:
                ws = _connect(ws_url)
                print(f"{stream_name} socket connected")
            except Exception as e:
                print(f"{stream_name} socket error: {e}")
                time.sleep(2)
                continue
        now = time.monotonic()
        if now < next_frame_time:
            time.sleep(next_frame_time - now)
        else:
            next_frame_time = now
        with frame_lock:
            frame = frame_store.get("frame")
        if frame is None:
            time.sleep(0.01)
            continue
        ok, buffer = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality],
        )
        if not ok:
            continue
        try:
            ws.send(buffer.tobytes(), opcode=0x2)
        except Exception as e:
            print(f"{stream_name} send error: {e}")
            try:
                ws.close()
            except Exception:
                pass
            ws = None
            time.sleep(1)
            continue
        next_frame_time += 1.0 / target_fps


def _thermal_sender():
    ws = None
    next_frame_time = time.monotonic()
    while True:
        if _mlx is None:
            time.sleep(2)
            continue
        if ws is None:
            try:
                ws = _connect(THERMAL_URL)
                print("Thermal socket connected")
            except Exception as e:
                print(f"Thermal socket error: {e}")
                time.sleep(2)
                continue
        now = time.monotonic()
        if now < next_frame_time:
            time.sleep(next_frame_time - now)
        else:
            next_frame_time = now
        temp = _get_thermal_frame()
        if temp is None:
            continue
        image = _thermal_to_image(temp)
        ok, buffer = cv2.imencode(
            ".jpg",
            image,
            [int(cv2.IMWRITE_JPEG_QUALITY), THERMAL_JPEG_QUALITY],
        )
        if not ok:
            continue
        try:
            ws.send(buffer.tobytes(), opcode=0x2)
        except Exception as e:
            print(f"Thermal send error: {e}")
            try:
                ws.close()
            except Exception:
                pass
            ws = None
            time.sleep(1)
            continue
        next_frame_time += 1.0 / THERMAL_FPS


def _setup_gpio():
    if GPIO is None:
        print("RPi.GPIO not available; motor control disabled")
        return
    GPIO.setmode(GPIO.BCM)
    for pin in MOTOR_PINS.values():
        GPIO.setup(pin, GPIO.OUT)
    GPIO.output(MOTOR_PINS["in1"], GPIO.LOW)
    GPIO.output(MOTOR_PINS["in2"], GPIO.LOW)
    GPIO.output(MOTOR_PINS["in3"], GPIO.LOW)
    GPIO.output(MOTOR_PINS["in4"], GPIO.LOW)
    GPIO.output(MOTOR_PINS["ena"], GPIO.HIGH)
    GPIO.output(MOTOR_PINS["enb"], GPIO.HIGH)
    try:
        pwm_a = GPIO.PWM(MOTOR_PINS["ena"], 1000)
        pwm_b = GPIO.PWM(MOTOR_PINS["enb"], 1000)
        base_duty = int((DEFAULT_SPEED / 255) * 100)
        pwm_a.start(base_duty)
        pwm_b.start(base_duty)
        globals()["_pwm_a"] = pwm_a
        globals()["_pwm_b"] = pwm_b
    except Exception:
        pass


def _set_drive_state(forward=None, turn=None):
    global _drive_forward, _drive_turn
    if forward is not None:
        _drive_forward = forward
    if turn is not None:
        _drive_turn = turn
    _apply_drive_state()


def _apply_drive_state():
    if GPIO is None:
        return
    forward = _drive_forward
    turn = _drive_turn
    if forward == 0 and turn == 0:
        _motor_stop()
        return
    if forward != 0 and turn == 0:
        if forward == 1:
            _motor_forward()
        else:
            _motor_back()
        return
    if forward == 0 and turn != 0:
        if turn == 1:
            _motor_right()
        else:
            _motor_left()
        return
    if forward == 1 and turn == 1:
        _motor_forward_right()
    elif forward == 1 and turn == -1:
        _motor_forward_left()
    elif forward == -1 and turn == 1:
        _motor_back_right()
    elif forward == -1 and turn == -1:
        _motor_back_left()


def _motor_forward():
    if GPIO is None:
        return
    GPIO.output(MOTOR_PINS["in1"], GPIO.HIGH)
    GPIO.output(MOTOR_PINS["in2"], GPIO.LOW)
    GPIO.output(MOTOR_PINS["in3"], GPIO.HIGH)
    GPIO.output(MOTOR_PINS["in4"], GPIO.LOW)


def _motor_back():
    if GPIO is None:
        return
    GPIO.output(MOTOR_PINS["in1"], GPIO.LOW)
    GPIO.output(MOTOR_PINS["in2"], GPIO.HIGH)
    GPIO.output(MOTOR_PINS["in3"], GPIO.LOW)
    GPIO.output(MOTOR_PINS["in4"], GPIO.HIGH)


def _motor_left():
    if GPIO is None:
        return
    GPIO.output(MOTOR_PINS["in1"], GPIO.LOW)
    GPIO.output(MOTOR_PINS["in2"], GPIO.HIGH)
    GPIO.output(MOTOR_PINS["in3"], GPIO.HIGH)
    GPIO.output(MOTOR_PINS["in4"], GPIO.LOW)


def _motor_right():
    if GPIO is None:
        return
    GPIO.output(MOTOR_PINS["in1"], GPIO.HIGH)
    GPIO.output(MOTOR_PINS["in2"], GPIO.LOW)
    GPIO.output(MOTOR_PINS["in3"], GPIO.LOW)
    GPIO.output(MOTOR_PINS["in4"], GPIO.HIGH)


def _motor_stop():
    if GPIO is None:
        return
    GPIO.output(MOTOR_PINS["in1"], GPIO.LOW)
    GPIO.output(MOTOR_PINS["in2"], GPIO.LOW)
    GPIO.output(MOTOR_PINS["in3"], GPIO.LOW)
    GPIO.output(MOTOR_PINS["in4"], GPIO.LOW)


def _set_speed(value):
    global LAST_SPEED
    if GPIO is None:
        return
    try:
        speed_value = int(value)
    except Exception:
        return
    speed_value = max(0, min(255, speed_value))
    LAST_SPEED = speed_value
    duty = int((speed_value / 255) * 100)
    pwm_a = globals().get("_pwm_a")
    pwm_b = globals().get("_pwm_b")
    if pwm_a and pwm_b:
        pwm_a.ChangeDutyCycle(duty)
        pwm_b.ChangeDutyCycle(duty)


def _motor_forward_right():
    if GPIO is None:
        return
    GPIO.output(MOTOR_PINS["in1"], GPIO.HIGH)
    GPIO.output(MOTOR_PINS["in2"], GPIO.LOW)
    GPIO.output(MOTOR_PINS["in3"], GPIO.LOW)
    GPIO.output(MOTOR_PINS["in4"], GPIO.LOW)


def _motor_forward_left():
    if GPIO is None:
        return
    GPIO.output(MOTOR_PINS["in1"], GPIO.LOW)
    GPIO.output(MOTOR_PINS["in2"], GPIO.LOW)
    GPIO.output(MOTOR_PINS["in3"], GPIO.HIGH)
    GPIO.output(MOTOR_PINS["in4"], GPIO.LOW)


def _motor_back_right():
    if GPIO is None:
        return
    GPIO.output(MOTOR_PINS["in1"], GPIO.LOW)
    GPIO.output(MOTOR_PINS["in2"], GPIO.HIGH)
    GPIO.output(MOTOR_PINS["in3"], GPIO.LOW)
    GPIO.output(MOTOR_PINS["in4"], GPIO.LOW)


def _motor_back_left():
    if GPIO is None:
        return
    GPIO.output(MOTOR_PINS["in1"], GPIO.LOW)
    GPIO.output(MOTOR_PINS["in2"], GPIO.LOW)
    GPIO.output(MOTOR_PINS["in3"], GPIO.LOW)
    GPIO.output(MOTOR_PINS["in4"], GPIO.HIGH)


if __name__ == "__main__":
    _register_robot()
    _setup_gpio()
    _init_mlx()
    usb_cap = _open_capture_with_fallbacks(FRAME_WIDTH, FRAME_HEIGHT, "USB")
    threading.Thread(
        target=_capture_loop,
        args=(usb_cap, _usb_frame_store, _usb_frame_lock),
        daemon=True,
    ).start()
    threading.Thread(target=_command_listener, daemon=True).start()
    threading.Thread(target=_telemetry_sender, daemon=True).start()
    threading.Thread(
        target=_frame_sender_latest,
        args=(_usb_frame_store, _usb_frame_lock, VIDEO_URL, "Video", TARGET_FPS, JPEG_QUALITY),
        daemon=True,
    ).start()
    threading.Thread(target=_thermal_sender, daemon=True).start()
    while True:
        time.sleep(1)
