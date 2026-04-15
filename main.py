import cv2
import time
import socketio

# --- Session state ---
focus_history = []
start_time = time.time()
alert_cooldown = 0
no_eye_streak = 0
productivity_ema = 72.0

sio = socketio.Client()


@sio.event
def connect():
    print("✅ Connected to Flask web server!")


@sio.event
def disconnect():
    print("❌ Disconnected from Flask web server")


def send_focus_data(status, score, total_time, ai_payload):
    try:
        sio.emit(
            "focus_update",
            {
                "status": status,
                "score": score,
                "total_time": total_time,
                "ai": ai_payload,
            },
        )
    except Exception:
        pass


def play_alert():
    global alert_cooldown
    if time.time() - alert_cooldown > 3:
        alert_cooldown = time.time()
        try:
            print("\a", flush=True)
        except Exception:
            print("🚨 ALERT: DISTRACTED! (Look at camera)")


def calculate_focus_score(eyes_detected):
    if eyes_detected == 2:
        return 100
    if eyes_detected == 1:
        return 60
    return 20


def analyze_ai_signals(fh, fw, x, y, w, h, eyes_in_face):
    """
    Lightweight CV heuristics (no extra models):
    - Phone / look-down: face sits low in frame or chin-down posture proxy.
    - Fatigue: frontal face present but no eyes detected for several frames (closed eyes
      or heavy downward gaze confuse Haar eyes similarly — we disambiguate with position).
    """
    global no_eye_streak

    area_ratio = (w * h) / float(fh * fw + 1e-6)
    face_bottom = (y + h) / float(fh)
    face_center_y = (y + h * 0.5) / float(fh)
    big_enough = area_ratio >= 0.012

    if eyes_in_face == 0 and big_enough:
        no_eye_streak += 1
    else:
        no_eye_streak = 0

    phone_risk = False
    if big_enough:
        if face_bottom > 0.68:
            phone_risk = True
        elif eyes_in_face == 0 and face_bottom > 0.60:
            phone_risk = True

    fatigue_risk = False
    if big_enough and not phone_risk:
        if eyes_in_face == 0 and no_eye_streak >= 18 and face_center_y < 0.58:
            fatigue_risk = True

    return phone_risk, fatigue_risk


def compute_productivity(avg_focus, phone_risk, fatigue_risk):
    global productivity_ema
    raw = float(avg_focus)
    if phone_risk:
        raw -= 38
    if fatigue_risk:
        raw -= 32
    if phone_risk and fatigue_risk:
        raw -= 12
    raw = max(0.0, min(100.0, raw))
    productivity_ema = 0.86 * productivity_ema + 0.14 * raw
    return int(round(productivity_ema))


def build_ai_hint(phone_risk, fatigue_risk):
    if phone_risk:
        return "Head down — possible phone use. Lift your gaze to the screen."
    if fatigue_risk:
        return "Eyes not visible — stretch, blink, or take a 20s break."
    return "Engaged — posture and attention look on track."


def main():
    global focus_history, no_eye_streak, productivity_ema

    try:
        sio.connect("http://localhost:5000")
    except Exception:
        print("⚠️  Flask server not running? Starting detection anyway...")

    face_cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    eye_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_eye.xml")

    cap = cv2.VideoCapture(0)
    print("👀 Focus Level AI + smart posture / fatigue heuristics")
    print("📹 Press 'q' to quit")

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame = cv2.flip(frame, 1)
        fh, fw = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        faces = face_cascade.detectMultiScale(gray, 1.3, 5)
        eyes_in_face = 0
        phone_risk = False
        fatigue_risk = False

        if len(faces) > 0:
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            cv2.rectangle(frame, (x, y), (x + w, y + h), (255, 0, 0), 2)

            roi_gray = gray[y : y + h, x : x + w]
            roi_color = frame[y : y + h, x : x + w]
            eyes = eye_cascade.detectMultiScale(roi_gray)
            eyes_in_face = min(len(eyes), 2)
            for (ex, ey, ew, eh) in eyes:
                cv2.rectangle(roi_color, (ex, ey), (ex + ew, ey + eh), (0, 255, 0), 2)

            phone_risk, fatigue_risk = analyze_ai_signals(fh, fw, x, y, w, h, eyes_in_face)

        focus_score = calculate_focus_score(eyes_in_face)
        focus_history.append(focus_score)
        if len(focus_history) > 60:
            focus_history.pop(0)

        avg_focus = sum(focus_history) / len(focus_history) if focus_history else 0
        productivity = compute_productivity(avg_focus, phone_risk, fatigue_risk)
        hint = build_ai_hint(phone_risk, fatigue_risk)

        if focus_score == 100:
            status = "🟢 FOCUSED"
        elif focus_score == 60:
            status = "🟡 PARTIALLY FOCUSED"
        else:
            status = "🔴 DISTRACTED"
            play_alert()

        total_time = time.time() - start_time
        display_time = int(total_time)

        tag = ""
        if phone_risk:
            tag = " | 📱 look-down"
        elif fatigue_risk:
            tag = " | 😴 fatigue?"

        cv2.putText(
            frame,
            f"{status}{tag}",
            (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (0, 255, 0),
            2,
        )
        cv2.putText(
            frame,
            f"Focus: {int(avg_focus)}%  |  Productivity: {productivity}%",
            (10, 64),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 0),
            2,
        )
        cv2.putText(
            frame,
            f"Time: {display_time // 60}:{display_time % 60:02d}",
            (10, 98),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (0, 255, 255),
            2,
        )
        cv2.putText(
            frame,
            "Press 'q' to quit",
            (10, fh - 12),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (255, 255, 255),
            1,
        )

        ai_payload = {
            "productivity": productivity,
            "phone_risk": phone_risk,
            "fatigue_risk": fatigue_risk,
            "hint": hint,
        }
        send_focus_data(status, int(avg_focus), display_time, ai_payload)

        cv2.imshow("Focus Level AI - Webcam", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()
    sio.disconnect()
    print("👋 Focus Level AI stopped!")


if __name__ == "__main__":
    main()
