import cv2
import time
import sys
import os
import glob
import random


# Auto-scan for an available Arduino serial port; return None if not found
def autodetect_port():  
    """Auto-detect a connected Arduino serial port"""
    cands = sorted(
        glob.glob('/dev/cu.usbmodem*') + glob.glob('/dev/cu.usbserial*') +
        glob.glob('/dev/tty.usbmodem*') + glob.glob('/dev/ttyACM*') + glob.glob('/dev/ttyUSB*'))
    return cands[0] if cands else None

try:
    # Serial library: used to control the motor; warns if absent but still runs for testing
    import serial  # pip3 install pyserial
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False

try:
    # Libraries for sound synthesis: numpy for math, sounddevice for real-time audio
    import numpy as np
    import sounddevice as sd  # pip3 install sounddevice numpy
    SOUND_AVAILABLE = True
except Exception:
    SOUND_AVAILABLE = False


# Does not affect functionality; only makes the terminal output look nice
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    GREY = '\033[90m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    RESET = '\033[0m'


C = {
    'GREEN': Colors.GREEN, 'RED': Colors.RED, 'GREY': Colors.GREY,
    'YELLOW': Colors.YELLOW, 'BLUE': Colors.BLUE, 'RESET': Colors.RESET
}


# Continuously synthesize and output sound from people count / motion / prediction-failure
class SoundEngine:
    # sample rate, block size, people needed for max breakdown, and synthesis state variables
    def __init__(self, enabled=True, max_faces=5, samplerate=44100, blocksize=1024):
        self.enabled = enabled and SOUND_AVAILABLE
        self.sr = samplerate
        self.bs = blocksize
        self.max_faces = max(1, max_faces)

        # Overtone multipliers vs. the base pitch, and the detune applied to overtones when prediction fails
        self.ratios = None       # Consonant interval ratios 
        self.inharm = None       # Per-overtone detune amount driven by discord

        
        self.tgt_intensity = 0.0
        self.tgt_motion = 0.0
        self.tgt_discord = 0.0
        self.cur_intensity = 0.0
        self.cur_motion = 0.0
        self.cur_discord = 0.0
        self.master = 0.0# master volume

        self.part_phase = None
        self.lfo_phase = 0.0
        self.trem_phase = 0.0
        self.drift_phase = 0.0
        self.stream = None

        if enabled and not SOUND_AVAILABLE:
            print(f"{C['YELLOW']}sounddevice/numpy not installed, sound disabled (pip3 install sounddevice numpy){C['RESET']}")

    
    # Open camera, start sound
    def start(self):
        if not self.enabled:
            return
        try:
            self.ratios = np.array([1.0, 1.5, 2.0, 2.5, 3.0, 4.0])
            self.inharm = np.array([0.0, 0.030, -0.025, 0.050, -0.040, 0.060])
            self.part_phase = np.zeros(len(self.ratios))
            self.stream = sd.OutputStream(
                samplerate=self.sr, channels=1, blocksize=self.bs,
                dtype='float32', callback=self._callback)
            self.stream.start()
            print(f"{C['GREEN']}Sound started{C['RESET']}")
        except Exception as e:
            print(f"{C['RED']}Sound start failed: {e}{C['RESET']}")
            self.enabled = False

    # Normalize people count / motion / prediction-failure and write to target parameters
    def update(self, face_count, motion, discord):
        self.tgt_intensity = min(1.0, face_count / float(self.max_faces))
        self.tgt_motion = max(0.0, min(1.0, motion))
        self.tgt_discord = max(0.0, min(1.0, discord))

    # Called automatically at high frequency by the audio thread
    def _callback(self, outdata, frames, time_info, status):
        sr = self.sr
        a = 0.08
        self.cur_intensity += (self.tgt_intensity - self.cur_intensity) * a
        self.cur_motion += (self.tgt_motion - self.cur_motion) * a
        self.cur_discord += (self.tgt_discord - self.cur_discord) * a
        # Target volume is 0 (mute) when no one is present , else 0.9; master
        target_master = 0.0 if self.tgt_intensity <= 0.001 else 0.9
        self.master += (target_master - self.master) * 0.03
        inten = self.cur_intensity
        mot = self.cur_motion
        dis = self.cur_discord

        n = np.arange(1, frames + 1)
        two_pi_sr = 2 * np.pi / sr

        # Slow random drift
        drift_inc = 2 * np.pi * 0.05 / sr
        drift = np.sin(self.drift_phase + drift_inc * n)
        self.drift_phase = (self.drift_phase + drift_inc * frames) % (2 * np.pi)

    
        # Base pitch rises with people: ~80Hz when few, ~220Hz when many, times a slow drift for variation
        base_pitch = (80.0 + inten * 140.0) * (1.0 + 0.03 * drift)

        # Vibrato
        vib_rate = 4.0 + mot * 8.0
        vib_inc = 2 * np.pi * vib_rate / sr
        lfo = np.sin(self.lfo_phase + vib_inc * n)
        self.lfo_phase = (self.lfo_phase + vib_inc * frames) % (2 * np.pi)
        # The more motion, the deeper the vibrato
        f_base = base_pitch * (1.0 + (0.008 + mot * 0.05) * lfo)

        # Accumulate
        sig = np.zeros(frames)
        NP = len(self.ratios)
        # The higher the discord, the more detuned and harsh the overtones 
        for k in range(NP):
            ratio = self.ratios[k] * (1.0 + dis * self.inharm[k])
            phase = self.part_phase[k] + two_pi_sr * np.cumsum(f_base * ratio)
            self.part_phase[k] = phase[-1] % (2 * np.pi)
            presence = np.clip(inten * NP - k, 0.0, 1.0)   # The more people, the fuller the overtones
            sig += (1.0 / (k + 1)) * presence * np.sin(phase)
        sig *= 0.28

        # Distortion: increases with discord
        # The more prediction fails, the more the sound breaks down
        if dis > 0.001:
            sig += (dis * 0.30) * (np.random.rand(frames) * 2 - 1)
        drive = 1.0 + dis * 8.0
        sig = np.tanh(sig * drive) / np.tanh(drive)

        
        trem_rate = 6.0 + mot * 10.0
        trem_inc = 2 * np.pi * trem_rate / sr
        trem = np.sin(self.trem_phase + trem_inc * n)
        self.trem_phase = (self.trem_phase + trem_inc * frames) % (2 * np.pi)
        sig *= (1.0 - (mot * 0.5) * (0.5 + 0.5 * trem))

        # Clip to [-1,1] to prevent clipping, plus buffering
        sig *= self.master
        np.clip(sig, -1.0, 1.0, out=sig)
        outdata[:, 0] = sig.astype(np.float32)

    # Stop and close the audio stream
    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop(); self.stream.close()
            except Exception:
                pass
            self.stream = None



# White fade in/out on loop or clip change
class VideoPlayer:
    PLAY, FADEOUT, FADEIN = 0, 1, 2

    def __init__(self, enabled=True, fullscreen=True, window="Video", fade_dur=0.4):
        self.enabled = enabled
        self.fullscreen = fullscreen
        self.window = window
        self.fade_dur = fade_dur
        self.cap = None
        self.current = None
        self.target = None
        self.fps = 5.0
        self.speed = 1.0
        self.win_created = False
        self.warned = set()
        self.state = self.PLAY
        self.last = None
        self.next_read = 0.0
        self.fade_start = 0.0
        self.total_frames = 0
        self.frame_idx = 0
        self._looped = False

    # Set the current video file to play; a different one triggers a clip transition
    def set_target(self, path):
        self.target = path

    # Return the current video playback progress
    def progress(self):
        return (self.frame_idx / self.total_frames) if self.total_frames > 0 else 0.0

    # Loop or clip change
    def pop_loop(self):
        r = self._looped
        self._looped = False
        return r

    # Fullscreen
    def _ensure_window(self):
        if not self.win_created:
            cv2.namedWindow(self.window, cv2.WINDOW_NORMAL)
            if self.fullscreen:
                cv2.setWindowProperty(self.window, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
            self.win_created = True

    # Fade in/out uses solid white
    def _fade_like(self, frame):
        import numpy as _np
        return _np.full_like(frame, 255)  

    # Open the video file
    def _open(self, path):
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            if path not in self.warned:
                print(f"{C['YELLOW']}Cannot open video: {path}{C['RESET']}")
                self.warned.add(path)
            self.cap = None
            self.current = path
            self.total_frames = 0
            self.frame_idx = 0
            return False
        self.cap = cap
        self.current = path
        f = cap.get(cv2.CAP_PROP_FPS)
        self.fps = f if f and f > 0 else 5.0
        self.total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        self.frame_idx = 0
        return True

    # Play at real speed
    def _interval(self):
        return 1.0 / (self.fps * max(0.1, self.speed))

    # Display a frame according to the state machine
    def render(self):
        if not self.enabled:
            return
        self._ensure_window()
        now = time.time()

        if self.cap is None and self.current is None and self.target is not None:
            if self._open(self.target):
                ret, f = self.cap.read()
                if ret:
                    self.last = f
                    self.frame_idx += 1
                    self.next_read = now + self._interval()
                self.state = self.FADEIN
                self.fade_start = now
            else:
                return

        disp = None

        # If the target clip changed, fade out; when finished, fade out then loop
        if self.state == self.PLAY:
            if self.target is not None and self.target != self.current:
                self.state = self.FADEOUT
                self.fade_start = now
                disp = self.last
            else:
                if now >= self.next_read and self.cap is not None:
                    ret, f = self.cap.read()
                    if ret:
                        self.last = f
                        self.frame_idx += 1
                        self.next_read = now + self._interval()
                    else:
                        self.state = self.FADEOUT
                        self.fade_start = now
                disp = self.last

        # Fade out
        elif self.state == self.FADEOUT:
            if self.last is None:
                self.state = self.FADEIN
                self.fade_start = now
            else:
                pr = min(1.0, (now - self.fade_start) / self.fade_dur)
                fadecol = self._fade_like(self.last)
                disp = cv2.addWeighted(self.last, 1.0 - pr, fadecol, pr, 0)
                if pr >= 1.0:
                    nxt = self.target if (self.target and self.target != self.current) else self.current
                    self._open(nxt)
                    self._looped = True     # Loop or clip change
                    if self.cap is not None:
                        ret, f = self.cap.read()
                        if ret:
                            self.last = f
                            self.frame_idx += 1
                            self.next_read = now + self._interval()
                    self.state = self.FADEIN
                    self.fade_start = now

        # Fade in
        elif self.state == self.FADEIN:
            if self.last is None:
                self.state = self.PLAY
            else:
                pr = min(1.0, (now - self.fade_start) / self.fade_dur)
                fadecol = self._fade_like(self.last)
                disp = cv2.addWeighted(fadecol, 1.0 - pr, self.last, pr, 0)
                if pr >= 1.0:
                    self.state = self.PLAY
                    self.next_read = now + self._interval()

        if disp is not None:
            cv2.imshow(self.window, disp)

    # Toggle fullscreen / windowed display
    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        if self.win_created:
            cv2.setWindowProperty(
                self.window, cv2.WND_PROP_FULLSCREEN,
                cv2.WINDOW_FULLSCREEN if self.fullscreen else cv2.WINDOW_NORMAL)

    # Release video resources
    def close(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None



# 'E' = trigger, 'S' = home
class MotorController:
    def __init__(self, port='/dev/cu.usbmodem', baud=9600, enabled=True):
        self.port = port
        self.baud = baud
        self.ser = None
        self.enabled = enabled and SERIAL_AVAILABLE
        if enabled and not SERIAL_AVAILABLE:
            print(f"{C['YELLOW']}pyserial not installed, motor control disabled (pip3 install pyserial){C['RESET']}")
            return
        if self.enabled:
            self._connect()

    # Open the serial port and wait for the Arduino; failure does not affect other parts
    def _connect(self):
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=1)
            time.sleep(2)
            print(f"{C['GREEN']}Motor serial connected: {self.port} @ {self.baud}{C['RESET']}")
        except Exception as e:
            print(f"{C['RED']}Cannot open serial port {self.port}: {e}{C['RESET']}")
            print(f"{C['GREY']}  Program keeps running but will not control the motor{C['RESET']}")
            self.ser = None
            self.enabled = False

    # Trigger one motor action
    def move(self):
        self._send(b'E')        # This firmware: 'E' triggers one alternating action from IDLE

    def person_left(self):
        self._send(b'S')        # no person

    # Read debug info printed back by the Arduino
    def read_debug(self):
        """check which motor the firmware is driving"""
        lines = []
        if self.ser and self.ser.is_open:
            try:
                while self.ser.in_waiting:
                    ln = self.ser.readline().decode('utf-8', 'ignore').strip()
                    if ln:
                        lines.append(ln)
            except Exception:
                pass
        return lines

    # Write bytes to the serial port
    def _send(self, data):
        if self.ser and self.ser.is_open:
            try:
                self.ser.write(data)
            except Exception as e:
                print(f"{C['RED']}Serial send failed: {e}{C['RESET']}")

    # Home the motor before closing the serial port on exit
    def close(self):
        if self.ser and self.ser.is_open:
            try:
                self.person_left()
                time.sleep(0.1)
                self.ser.close()
                print(f"{C['GREY']}Motor serial closed{C['RESET']}")
            except Exception:
                pass



class FaceDetector:
    def __init__(self, camera_id=0, motor_port='/dev/cu.usbmodem',
                 motor_enabled=True, sound_enabled=True,
                 video_enabled=True, video_many='1.mp4', video_few='mei.mp4',
                 mei_speed=2.0, min_face_size=(40, 40), cooldown=2.0):
        self.camera_id = camera_id
        self.min_face_size = min_face_size
        self.cooldown = cooldown
        self.cap = None
        self.running = False

        self.motor = MotorController(port=motor_port, enabled=motor_enabled)
        self.sound = SoundEngine(enabled=sound_enabled)
        self.video = VideoPlayer(enabled=video_enabled, fullscreen=True)
        self.video_many = video_many      # played when people count > threshold
        self.video_few = video_few        # played when people count <= threshold
        # People threshold: >=3 plays video 1, otherwise plays mei
        self.video_threshold = 3
        self.mei_speed = mei_speed        # mei playback speed multiplier 
        self.mei_triggered = True         # whether mei has triggered this round
        self.next_v1_trigger = 0.0        # next trigger time for video 1
        self.prev_in_v1 = False           # whether the previous frame was on video 1
        self.v1_min = 5.0                 
        self.v1_max = 10.0                
        self.motor_moving = False         # whether a triggered action is currently in progress
        self.move_start = 0.0
        # Timed-motion state machine: idle=waiting, rearm=preparing, moving=in progress; ensures one action finishes before the next
        self.MOVE_DURATION = 7.3          # duration of one extend-retract cycle
        self.motor_state = 'idle'         # 'idle' | 'moving' | 'rearm'
        self.move_start_t = 0.0
        self.next_move_t = 0.0

        self.face_count = 0
        self.last_face_time = 0
        self.last_heartbeat = 0
        self.motor_active = False

        # Motion amount
        self.prev_cx = None
        self.prev_cy = None
        self.motion = 0.0

        
        # Predictor
        self.pnx = None            # one-step prediction made last frame for this frame
        self.pny = None
        self.vel_x = 0.0
        self.vel_y = 0.0
        self.disp_x = None         # display point
        self.disp_y = None
        self.pred_err = 0.0
        self.discord = 0.0         # smoothed prediction-failure level
        self.prev_face_count = 0
        self.pred_tol = 25.0       # error tolerance (px): below this = prediction success
        self.pred_horizon = 8      # frames ahead for display

        # Load the face-detection model
        self.face_cascade = self._load_cascade()
        self.scale_factor = 1.05   # sensitivity
        self.min_neighbors = 3

        self.fps = 0
        self.frame_count = 0
        self.fps_start = time.time()

        print(f"{C['GREEN']}Face detector ready{C['RESET']}")
        print(f"{C['GREY']}  Camera: {camera_id}  Motor: {motor_port} ({'enabled' if self.motor.enabled else 'disabled'})  Sound: {'enabled' if self.sound.enabled else 'disabled'}{C['RESET']}")
        print(f"{C['GREY']}q/ESC quit | s screenshot | m force-person | b force-none{C['RESET']}")

    def _pulse_move(self, now):
        # Send 'E' to trigger one action; ignored if one is already running to avoid overlap
        if not self.motor_moving:
            self.motor.move()             # 'E'
            self.motor_moving = True
            self.move_start = now

    # Load the Haar face cascade classifier model file
    def _load_cascade(self):
        cascade_file = 'haarcascade_frontalface_default.xml'
        if not os.path.exists(cascade_file):
            cascade_file = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
            if not os.path.exists(cascade_file):
                raise RuntimeError("Face detection model file not found")
        cascade = cv2.CascadeClassifier(cascade_file)
        if cascade.empty():
            raise RuntimeError("Failed to load model")
        print(f"{C['GREEN']}Model loaded successfully{C['RESET']}")
        return cascade

    def start(self):
        if self.running:
            return
        try:
            self.cap = cv2.VideoCapture(self.camera_id)
            if not self.cap.isOpened():
                raise RuntimeError(f"Cannot open camera {self.camera_id}")
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            self.running = True
            print(f"{C['GREEN']}Camera started{C['RESET']}")
            cv2.namedWindow("Face Detection", cv2.WINDOW_NORMAL)
            self.sound.start()
            self._detect_loop()
        except Exception as e:
            print(f"{C['RED']}Startup failed: {e}{C['RESET']}")
            if self.cap:
                self.cap.release()
                self.cap = None
            cv2.destroyAllWindows()

    # Main loop
    def _detect_loop(self):
        print(f"{C['BLUE']}The more people, the more intense the motion{C['RESET']}")

        while self.running:
            try:
                ret, frame = self.cap.read()
                if not ret:
                    time.sleep(0.1)
                    continue

                frame = cv2.flip(frame, 1)
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                # Detect faces on the grayscale image; faces is the list of detected face boxes
                faces = self.face_cascade.detectMultiScale(
                    gray, scaleFactor=self.scale_factor,
                    minNeighbors=self.min_neighbors, minSize=self.min_face_size)
                self.face_count = len(faces)
                now = time.time()

                
                # Prediction
                if self.face_count > 0:
                    cx = float(sum(x + w / 2 for (x, y, w, h) in faces)) / self.face_count
                    cy = float(sum(y + h / 2 for (x, y, w, h) in faces)) / self.face_count

                    if self.prev_cx is not None and self.face_count == self.prev_face_count:
                        dist = ((cx - self.prev_cx) ** 2 + (cy - self.prev_cy) ** 2) ** 0.5
                        self.motion += (min(1.0, dist / 60.0) - self.motion) * 0.3
                        # Prediction error: last frame's one-step prediction vs. actual
                        if self.pnx is not None:
                            err = ((cx - self.pnx) ** 2 + (cy - self.pny) ** 2) ** 0.5
                            self.pred_err = min(1.0, err / self.pred_tol)
                            self.discord += (self.pred_err - self.discord) * 0.15
                    
                        # Estimate velocity with exponential smoothing 
                        self.vel_x += ((cx - self.prev_cx) - self.vel_x) * 0.4
                        self.vel_y += ((cy - self.prev_cy) - self.vel_y) * 0.4
                        self.pnx = cx + self.vel_x
                        self.pny = cy + self.vel_y
                        self.disp_x = cx + self.vel_x * self.pred_horizon
                        self.disp_y = cy + self.vel_y * self.pred_horizon
                    else:
                        self.pnx = self.pny = None
                        self.vel_x = self.vel_y = 0.0
                        self.disp_x = self.disp_y = None

                    self.prev_cx, self.prev_cy = cx, cy

                    # Overlay
                    cv2.circle(frame, (int(cx), int(cy)), 6, (0, 255, 0), -1)
                    if self.disp_x is not None:
                        px, py = int(self.disp_x), int(self.disp_y)
                        cv2.circle(frame, (px, py), 12, (0, 255, 255), 2)
                        cv2.line(frame, (int(cx), int(cy)), (px, py), (0, 255, 255), 1)
                else:
                    self.prev_cx = self.prev_cy = None
                    self.pnx = self.pny = None
                    self.disp_x = self.disp_y = None
                    self.motion += (0.0 - self.motion) * 0.1
                    self.discord += (0.0 - self.discord) * 0.05   # recover
                self.prev_face_count = self.face_count

                
                self.sound.update(self.face_count, self.motion, self.discord)

                
                # Person activate motor; no person home
                if self.face_count > 0:
                    self.last_face_time = now
                    if not self.motor_active:
                        print(f"{C['GREEN']}Person detected, starting timed motion{C['RESET']}")
                        self.motor_active = True
                        self.motor_state = 'idle'
                        self.next_move_t = now         # trigger the first one immediately
                else:
                    if now - self.last_face_time > self.cooldown and self.motor_active:
                        print(f"{C['YELLOW']}No person -> home{C['RESET']}")
                        self.motor.person_left()
                        self.motor_active = False
                        self.motor_state = 'idle'

               
                # people > 3 plays 1, otherwise plays mei
                try:
                    if self.face_count >= self.video_threshold:
                        self.video.speed = 1.0
                        self.video.set_target(self.video_many)
                    else:
                        self.video.speed = self.mei_speed
                        self.video.set_target(self.video_few)
                    self.video.render()
                except Exception as ve:
                    print(f"{C['YELLOW']}Video render error (ignored, does not affect motor): {ve}{C['RESET']}")

               
                # Timed
                try:
                    if self.motor_active:
                        interval = 5.0 if self.face_count > 3 else 8.0
                        if self.motor_state == 'moving':
                            if now - self.move_start_t >= self.MOVE_DURATION:
                                self.motor_state = 'idle'        # one full action finished
                        elif self.motor_state == 'rearm':
                            self.motor.move()                    
                            self.motor_state = 'moving'
                            self.move_start_t = now
                            self.next_move_t = now + interval
                            print(f"{C['BLUE']}motor moves once (interval {interval:.0f}s){C['RESET']}")
                        elif self.motor_state == 'idle':
                            if now >= self.next_move_t:
                                self.motor.person_left()         
                                self.motor_state = 'rearm'       # send 'E' next frame
                except Exception:
                    pass

                # See which motor is driven
                try:
                    for _ln in self.motor.read_debug():
                        print(f"{C['GREY']}[Arduino] {_ln}{C['RESET']}")
                except Exception:
                    pass

                for (x, y, w, h) in faces:
                    cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 200, 0), 1)

                
                col = (0, 255, 0) if self.face_count > 0 else (0, 0, 255)
                cv2.putText(frame, f"Faces: {self.face_count}", (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, col, 2)
                cv2.putText(frame, "Motor: ACTIVE" if self.motor_active else "Motor: home",
                            (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
                snd = "ON" if (self.sound and self.sound.enabled) else "off"
                cv2.putText(frame, f"Sound: {snd}  Motion: {self.motion:0.2f}",
                            (10, 88), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 255), 1)
                if self.face_count > 0:
                    ok = self.discord < 0.4
                    cv2.putText(frame, f"{'PREDICT OK' if ok else 'BREAKDOWN'}  discord:{self.discord:0.2f}",
                                (10, 118), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                                (0, 255, 0) if ok else (0, 0, 255), 2)
                cv2.putText(frame, f"FPS: {self.fps}", (10, 148),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

                cv2.imshow("Face Detection", frame)

                self.frame_count += 1
                if now - self.fps_start >= 1.0:
                    self.fps = self.frame_count
                    self.frame_count = 0
                    self.fps_start = now

                # q/ESC quit, s screenshot, m manual motor trigger, b manual home, f toggle fullscreen
                key = cv2.waitKey(1) & 0xFF
                if key == ord('q') or key == 27:
                    self.running = False
                    break
                elif key == ord('s'):
                    fn = f"face_capture_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
                    cv2.imwrite(fn, frame)
                    print(f"{C['GREEN']}Screenshot saved: {fn}{C['RESET']}")
                elif key == ord('m'):
                    self.motor_active = True   # manual start
                    self.motor_state = 'idle'
                    self.next_move_t = now
                elif key == ord('b'):
                    self.motor.person_left()   # manual home
                    self.motor_active = False
                elif key == ord('f'):
                    self.video.toggle_fullscreen()

                time.sleep(0.02)

            except Exception as e:
                print(f"{C['RED']}Detection error: {e}{C['RESET']}")
                time.sleep(0.1)
                continue

        self._cleanup()

    # Stop sound, close video and serial, release camera, close windows
    def _cleanup(self):
        print(f"{C['GREY']}Cleaning up...{C['RESET']}")
        if getattr(self, 'sound', None):
            self.sound.stop()
        if getattr(self, 'video', None):
            self.video.close()
        if self.motor:
            self.motor.close()
        if self.cap:
            self.cap.release()
            self.cap = None
        cv2.destroyAllWindows()
        print(f"{C['GREY']}Stopped{C['RESET']}")

    def stop(self):
        self.running = False
        time.sleep(0.5)
        self._cleanup()



# Create FaceDetector and run
def main():
    camera_id = 0
    motor_port = 'auto'
    motor_enabled = True
    sound_enabled = True
    video_enabled = True
    video_many = 'code/1.mp4'
    video_few = 'code/mei.mp4'
    mei_speed = 2.0

    # Parse command-line arguments
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == '--camera' and i + 1 < len(args):
            try:
                camera_id = int(args[i + 1])
            except ValueError:
                print(f"{C['RED']}Invalid camera ID{C['RESET']}")
                return
        elif arg == '--port' and i + 1 < len(args):
            motor_port = args[i + 1]
        elif arg == '--no-motor':
            motor_enabled = False
        elif arg == '--no-sound':
            sound_enabled = False
        elif arg == '--no-video':
            video_enabled = False
        elif arg == '--video-many' and i + 1 < len(args):
            video_many = args[i + 1]
        elif arg == '--video-few' and i + 1 < len(args):
            video_few = args[i + 1]
        elif arg == '--mei-speed' and i + 1 < len(args):
            try:
                mei_speed = float(args[i + 1])
            except ValueError:
                print(f"{C['RED']}Invalid mei-speed{C['RESET']}")
                return
        elif arg in ('--help', '-h'):
            print("open")
            print("  --camera ID   ")
            print("  --port PORT   ")
            print("  --no-motor    ")
            print("  --no-sound    ")
            print("  --no-video    ")
            print("  --video-many PATH ")
            print("  --video-few  PATH ")
            print("  --mei-speed  N  ")
            print("  --help, -h   ")
            print("\n q/ESC quit | s screenshot | m force-person | b force-none | f toggle video fullscreen")
            return

  
    # Auto-detect the serial port when unspecified; disable motor if none found
    if motor_port == 'auto':
        found = autodetect_port()
        if found:
            motor_port = found
            print(f"{C['GREEN']}Auto-detected Arduino serial port: {found}{C['RESET']}")
        else:
            print(f"{C['YELLOW']}No Arduino serial port detected (plug in the board or use --port); motor disabled{C['RESET']}")
            motor_enabled = False

    print(f"{C['GREY']}Starting... port: {motor_port}{C['RESET']}")
    detector = None
    try:
        detector = FaceDetector(
            camera_id=camera_id, motor_port=motor_port,
            motor_enabled=motor_enabled, sound_enabled=sound_enabled,
            video_enabled=video_enabled, video_many=video_many, video_few=video_few,
            mei_speed=mei_speed, cooldown=2.0)
        detector.start()
    except KeyboardInterrupt:
        print(f"\n{C['GREY']}Goodbye!{C['RESET']}")
    except Exception as e:
        print(f"{C['RED']}Program error: {e}{C['RESET']}")
        import traceback
        traceback.print_exc()
    finally:
        if detector:
            detector.stop()


if __name__ == "__main__":
    main()