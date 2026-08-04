#!/usr/bin/env python3
"""HEIMDALL 녹음기 — 가벼운 클라이언트

이 맥은 녹음만 합니다. 전사와 회의록은 사무실 처리 컴퓨터가 만듭니다.
그래서 Whisper 도, 큰 모델도 필요 없습니다.

  회의 녹음 시작 → 회의 진행 → 회의 녹음 종료 → 자동 업로드 → 잠시 뒤 회의록 완성
"""
import hashlib, json, os, re, shutil, subprocess, sys, threading, time, datetime
import urllib.error, urllib.parse, urllib.request, webbrowser

# 이 숫자를 올리면 이미 깔린 녹음기들이 「업데이트 있음」 을 표시합니다
VERSION = "1.8"

HOME = os.path.dirname(os.path.abspath(__file__))
CONF_DIR = os.path.join(os.path.expanduser("~"), ".heimdall")
os.makedirs(CONF_DIR, exist_ok=True)

try:
    import rumps
except ImportError:
    sys.exit("rumps 가 없습니다. 설치 프로그램을 다시 실행해주세요.")


# ─────────────────────────────────────────── 설정
def conf():
    p = os.path.join(HOME, "settings.json")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}


CONF = conf()
SB_URL = CONF.get("supabase_url", "").rstrip("/")
SB_KEY = CONF.get("supabase_anon_key", "")
WEB_URL = CONF.get("web_url", "")
ICON = os.path.join(HOME, "menubarTemplate.png")
SESSION = os.path.join(CONF_DIR, "session.json")


# ─────────────────────────────────────────── 서버
def api(path, method="GET", body=None, token=None, base="rest/v1"):
    u = f"{SB_URL}/{base}/{path}"
    h = {"apikey": SB_KEY, "Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(u, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw.strip() else None


class Auth:
    def __init__(self):
        self.access = self.refresh = None
        self.email = ""
        self.load()

    def load(self):
        if os.path.exists(SESSION):
            try:
                d = json.load(open(SESSION, encoding="utf-8"))
                self.refresh, self.email = d.get("refresh"), d.get("email", "")
                self.renew()
            except Exception:
                pass

    def save(self):
        json.dump({"refresh": self.refresh, "email": self.email},
                  open(SESSION, "w", encoding="utf-8"))
        try:
            os.chmod(SESSION, 0o600)
        except Exception:
            pass

    def login(self, email, password):
        d = api("token?grant_type=password", "POST",
                {"email": email, "password": password}, base="auth/v1")
        self.access, self.refresh = d["access_token"], d["refresh_token"]
        self.email = email
        self.save()

    def renew(self):
        if not self.refresh:
            return False
        try:
            d = api("token?grant_type=refresh_token", "POST",
                    {"refresh_token": self.refresh}, base="auth/v1")
            self.access, self.refresh = d["access_token"], d["refresh_token"]
            self.save()
            return True
        except Exception:
            self.access = None
            return False

    def token(self):
        if not self.access and not self.renew():
            return None
        return self.access

    def logout(self):
        self.access = self.refresh = None
        self.email = ""
        try:
            os.remove(SESSION)
        except Exception:
            pass

    def me(self):
        t = self.token()
        if not t:
            return None
        try:
            r = api("hd_member?select=name,approved,role&limit=1", token=t)
            return r[0] if r else None
        except Exception:
            return None


# ─────────────────────────────────────────── 녹음
def ffmpeg_path():
    for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg",
              os.path.join(HOME, "ffmpeg")):
        if os.path.exists(p):
            return p
    from shutil import which
    return which("ffmpeg")


def find_mic():
    """Jabra 를 우선으로 찾습니다. ffmpeg 이 있으면 장치 번호까지 고릅니다."""
    fp = ffmpeg_path()
    if not fp:
        return None, "기본 입력"
    try:
        out = subprocess.run([fp, "-hide_banner", "-f", "avfoundation",
                              "-list_devices", "true", "-i", ""],
                             capture_output=True, text=True).stderr
        seen, first = False, None
        for line in out.splitlines():
            if "AVFoundation audio devices" in line:
                seen = True; continue
            if not seen:
                continue
            m = re.search(r"\[(\d+)\]\s+(.+?)\s*$", line)
            if not m:
                continue
            if first is None:
                first = (m.group(1), m.group(2))
            if re.search(r"jabra|speak2", m.group(2), re.I):
                return m.group(1), m.group(2)
        if first:
            return first
    except Exception:
        pass
    return None, "기본 입력"


class Recorder:
    """ffmpeg 이 있으면 장치를 지정해 녹음하고, 없으면 macOS 기본 입력으로 녹음합니다."""

    def __init__(self):
        self.proc = None
        self.av = None
        self.path = None

    def start(self, path):
        self.path = path
        fp, (dev, _name) = ffmpeg_path(), find_mic()
        if fp and dev is not None:
            self.proc = subprocess.Popen(
                [fp, "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "avfoundation", "-i", f":{dev}",
                 "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "64k", path],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            return True
        # ffmpeg 이 없을 때 — macOS 내장 녹음기
        try:
            from AVFoundation import AVAudioRecorder
            from Foundation import NSURL
            settings = {"AVFormatIDKey": 1633772320,      # 'aac '
                        "AVSampleRateKey": 16000.0,
                        "AVNumberOfChannelsKey": 1,
                        "AVEncoderBitRateKey": 64000}
            url = NSURL.fileURLWithPath_(path)
            rec, err = AVAudioRecorder.alloc().initWithURL_settings_error_(url, settings, None)
            if rec is None:
                raise RuntimeError(str(err))
            rec.record()
            self.av = rec
            return True
        except Exception as e:
            raise RuntimeError(f"녹음을 시작하지 못했습니다: {e}")

    def stop(self):
        if self.proc:
            try:
                self.proc.stdin.write(b"q"); self.proc.stdin.flush()
                self.proc.wait(timeout=15)
            except Exception:
                try:
                    import signal
                    self.proc.send_signal(signal.SIGINT); self.proc.wait(timeout=10)
                except Exception:
                    self.proc.kill()
            self.proc = None
        if self.av:
            self.av.stop(); self.av = None


# ─────────────────────────────────────────── 입력 창
# rumps 의 입력창은 엔터를 누르면 바로 확인으로 넘어가 여러 줄을 받을 수 없습니다.
# 그래서 맥 기본 대화상자를 씁니다. 비밀번호는 점으로 가려집니다.
def _q(t):
    """AppleScript 문자열. 한글을 그대로 넣습니다."""
    return json.dumps(t, ensure_ascii=False)


def ask(prompt, title="HEIMDALL", default="", secret=False, ok="확인"):
    script = (
        f"display dialog {_q(prompt)} "
        f"with title {_q(title)} "
        f"default answer {_q(default)} "
        f"buttons {{{_q('취소')}, {_q(ok)}}} default button 2"
        + (" with hidden answer" if secret else "")
    )
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=300)
    except Exception:
        return None
    if r.returncode != 0:
        return None                      # 취소를 눌렀습니다
    m = re.search(r"text returned:(.*)$", r.stdout.strip())
    return m.group(1) if m else ""


def say_ok(title, msg):
    try:
        subprocess.run(["osascript", "-e",
                        f"display dialog {_q(msg)} with title {_q(title)} "
                        f"buttons {{{_q('확인')}}} default button 1"],
                       capture_output=True, timeout=120)
    except Exception:
        pass


# ─────────────────────────────────────────── 스스로 자동 실행 등록하기
# 맥을 켤 때마다 뜨게 하고, 꺼지면 다시 살아나게 합니다.
# 프로그램이 직접 등록하므로, 나중에 이 부분이 바뀌어도 업데이트만으로 고쳐집니다.
LABEL = "group.almighty.heimdall.recorder"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")

PLIST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array><string>{py}</string><string>{script}</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
"""


def agent_running():
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        return LABEL in out
    except Exception:
        return False


def ensure_autostart():
    """자동 실행이 등록돼 있는지 확인하고, 없으면 등록합니다.
    터미널이나 앱에서 직접 띄운 경우에는 서비스 쪽에 넘기고 이 창은 물러납니다."""
    want = PLIST_XML.format(label=LABEL, py=sys.executable,
                            script=os.path.join(HOME, "client.py"),
                            log=os.path.join(CONF_DIR, "log.txt"))
    have = ""
    if os.path.exists(PLIST):
        try:
            have = open(PLIST, encoding="utf-8").read()
        except Exception:
            pass
    try:
        if have.strip() != want.strip():
            os.makedirs(os.path.dirname(PLIST), exist_ok=True)
            open(PLIST, "w", encoding="utf-8").write(want)
            subprocess.run(["launchctl", "unload", PLIST], capture_output=True)
            subprocess.run(["launchctl", "load", PLIST], capture_output=True)
        elif not agent_running():
            subprocess.run(["launchctl", "load", PLIST], capture_output=True)
    except Exception:
        return

    # 서비스가 아닌 곳(터미널 등)에서 띄운 것이라면 서비스에 맡기고 물러납니다.
    # 그래야 번개 아이콘이 두 개 뜨지 않습니다.
    if os.getppid() != 1 and agent_running():
        try:
            subprocess.run(["launchctl", "kickstart", "-k",
                            f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        except Exception:
            pass
        sys.exit(0)


# ─────────────────────────────────────────── 스스로 갱신하기
# 웹앱과 같은 자리에 최신 파일과 버전 정보를 올려둡니다.
# 키가 들어 있지 않은 파일이라 공개되어도 안전합니다.
def vnum(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return (0,)


def check_update():
    """새 버전이 있으면 정보를 돌려줍니다."""
    base = (WEB_URL or "").rstrip("/")
    if not base:
        return None
    try:
        u = f"{base}/client-version.json?t={int(time.time())}"
        with urllib.request.urlopen(u, timeout=15) as r:
            info = json.load(r)
    except Exception:
        return None
    if vnum(info.get("version")) <= vnum(VERSION):
        return None
    if not str(info.get("url", "")).startswith(base):
        return None          # 우리 주소에서 온 파일만 받습니다
    return info


def fetch(url, sha=""):
    with urllib.request.urlopen(url + f"?t={int(time.time())}", timeout=60) as r:
        data = r.read()
    if sha and sha.lower() != hashlib.sha256(data).hexdigest():
        raise RuntimeError("받은 파일이 손상되었습니다.")
    return data


def apply_update(info):
    """새 파일을 받아 바꿔 끼웁니다. 아이콘 같은 곁들이 파일도 함께 받습니다."""
    data = fetch(info["url"], info.get("sha256", ""))
    if b"class Client" not in data or len(data) < 3000:
        raise RuntimeError("받은 파일이 녹음기 프로그램이 아닙니다.")
    target = os.path.join(HOME, "client.py")
    shutil.copy2(target, target + ".bak")
    with open(target, "wb") as f:
        f.write(data)

    # 아이콘은 없어도 그만이므로 실패해도 넘어갑니다
    base = (WEB_URL or "").rstrip("/")
    for item in (info.get("files") or []):
        try:
            u = item.get("url", "")
            if not u.startswith(base):
                continue
            name = os.path.basename(item.get("name") or u.split("/")[-1])
            if not re.fullmatch(r"[A-Za-z0-9@._-]+", name):
                continue
            open(os.path.join(HOME, name), "wb").write(fetch(u, item.get("sha256", "")))
        except Exception:
            pass
    return True


def relaunch():
    """앱을 다시 띄우고 자신은 종료합니다."""
    app = os.path.expanduser("~/Applications/HEIMDALL 녹음기.app")
    try:
        if os.path.isdir(app):
            subprocess.Popen(["open", "-n", app])
        else:
            subprocess.Popen([sys.executable, os.path.join(HOME, "client.py")])
    except Exception:
        pass


# ─────────────────────────────────────────── 앱
class Client(rumps.App):
    def __init__(self):
        super().__init__("", icon=ICON if os.path.exists(ICON) else None,
                         template=True, quit_button=None)
        self.auth = Auth()
        self.rec = Recorder()
        self.t0 = None
        self.path = None
        self.title_text = ""
        self.busy = False
        self.parts = self._load("participants")

        self.m_rec = rumps.MenuItem("회의 녹음 시작", callback=self.toggle)
        self.m_state = rumps.MenuItem("준비 중")
        self.m_who = rumps.MenuItem("로그인…", callback=self.login)
        self.m_parts = rumps.MenuItem("참석자 설정…", callback=self.set_parts)
        self.m_web = rumps.MenuItem("회의록 보러 가기", callback=self.open_web)
        self.m_jobs = rumps.MenuItem("처리 상태 확인", callback=self.check_jobs)
        self.m_up = rumps.MenuItem("", callback=self.do_update)
        self.m_up.hidden = True
        self.newver = None
        self.menu = [self.m_rec, self.m_state, None, self.m_up, self.m_web, self.m_jobs,
                     None, self.m_parts, self.m_who, None,
                     rumps.MenuItem(f"버전 {VERSION}"),
                     rumps.MenuItem("종료", callback=self.bye)]
        self.menu[f"버전 {VERSION}"].set_callback(None)
        self.timer = rumps.Timer(self.tick, 1)
        self.uptimer = rumps.Timer(lambda _: threading.Thread(
            target=self.look_for_update, daemon=True).start(), 6 * 3600)
        self.uptimer.start()
        threading.Thread(target=self.first_run, daemon=True).start()

    def first_run(self):
        self.refresh_who()
        self.look_for_update()
        if not self.auth.token():
            self.notify("로그인이 필요합니다",
                        "번개 아이콘 → 「로그인…」. 계정이 없으면 「로그인이 필요합니다」를 눌러 가입하세요.")

    # 작은 저장소
    def _p(self, k): return os.path.join(CONF_DIR, k)
    def _load(self, k):
        return open(self._p(k), encoding="utf-8").read().strip() if os.path.exists(self._p(k)) else ""
    def _save(self, k, v): open(self._p(k), "w", encoding="utf-8").write(v)

    def look_for_update(self):
        info = check_update()
        if not info:
            return
        self.newver = info
        self.m_up.title = f"업데이트 있음 ({info['version']}) — 눌러서 설치"
        self.m_up.hidden = False
        self.notify("새 버전이 있습니다", "번개 아이콘 → 「업데이트 있음」 을 눌러주세요.")

    def do_update(self, _):
        if not self.newver:
            return
        if self.rec.proc or self.rec.av or self.busy:
            rumps.alert("지금은 어렵습니다", "녹음이나 업로드가 끝난 뒤에 눌러주세요.")
            return
        try:
            apply_update(self.newver)
        except Exception as e:
            rumps.alert("업데이트에 실패했습니다", str(e))
            return
        rumps.alert("업데이트했습니다",
                    f"{self.newver['version']} 버전으로 바뀌었습니다.\n"
                    "확인을 누르면 다시 시작합니다.")
        relaunch()
        rumps.quit_application()

    def notify(self, t, m):
        try:
            rumps.notification("HEIMDALL", t, m)
        except Exception:
            pass

    def refresh_who(self):
        """상태 줄을 갱신합니다.
        로그인 전·승인 대기 중에는 눌러서 웹으로 갈 수 있게 해둡니다."""
        me = self.auth.me()
        if not me:
            self.m_state.title = ("로그인이 필요합니다 — 눌러서 가입하기" if WEB_URL
                                  else "설치가 덜 되었습니다 — 관리자에게 문의")
            self.m_state.set_callback(self.open_web)
            self.m_who.title = "로그인…"
        elif not me.get("approved"):
            self.m_state.title = "가입 승인 대기 중 — 눌러서 확인하기"
            self.m_state.set_callback(self.open_web)
            self.m_who.title = f"{self.auth.email} · 로그아웃"
        else:
            self.m_state.title = "대기 중"
            self.m_state.set_callback(None)
            self.m_who.title = f"{me.get('name') or self.auth.email} · 로그아웃"

    # 로그인
    def login(self, _):
        if self.auth.token():
            self.auth.logout(); self.refresh_who(); return

        email = ask("웹에서 가입하신 이메일을 적어주세요", "HEIMDALL 로그인",
                    default=self._load("email"), ok="다음")
        if email is None:
            return
        email = email.strip()
        if not email:
            say_ok("HEIMDALL", "이메일을 적어주세요.")
            return

        pw = ask(f"{email}\n비밀번호를 적어주세요", "HEIMDALL 로그인",
                 secret=True, ok="로그인")
        if pw is None:
            return
        if not pw:
            say_ok("HEIMDALL", "비밀번호를 적어주세요.")
            return

        try:
            self.auth.login(email, pw)
        except urllib.error.HTTPError:
            say_ok("로그인 실패", "이메일 또는 비밀번호가 맞지 않습니다.\n"
                                "웹에서 가입은 하셨는지 확인해주세요.")
            return
        except Exception as e:
            say_ok("로그인 실패", str(e))
            return

        self._save("email", email)
        self.refresh_who()
        me = self.auth.me() or {}
        if me.get("approved"):
            say_ok("로그인했습니다", f"{me.get('name') or email} 님, 이제 녹음하실 수 있습니다.")
        else:
            say_ok("가입 승인 대기 중",
                   "관리자가 승인하면 녹음하실 수 있습니다.\n"
                   "번개 아이콘의 상태 줄을 눌러 확인하실 수 있습니다.")

    # 녹음
    def toggle(self, _):
        if self.busy:
            rumps.alert("올리는 중입니다", "이전 녹음을 올리고 있습니다. 잠시만 기다려주세요.")
            return
        if self.rec.proc or self.rec.av:
            self.stop()
        else:
            self.start()

    def start(self):
        if not self.auth.token():
            rumps.alert("로그인이 필요합니다", "메뉴에서 로그인해주세요.")
            return
        default = datetime.date.today().strftime("%y%m%d") + " 회의"
        t = ask("회의명을 적어주세요", "HEIMDALL", default=default, ok="녹음 시작")
        if t is None:
            return
        self.title_text = t.strip() or default
        self.path = os.path.join(CONF_DIR, f"rec_{int(time.time())}.m4a")
        try:
            self.rec.start(self.path)
        except Exception as e:
            rumps.alert("녹음을 시작하지 못했습니다", str(e))
            return
        self.t0 = time.time()
        self.m_rec.title = "회의 녹음 종료"
        self.m_state.title = f"녹음 중 · {self.title_text}"
        self.m_state.set_callback(None)
        self.timer.start()
        self.notify("녹음을 시작했습니다", self.title_text)

    def stop(self):
        self.timer.stop()
        self.rec.stop()
        self.title = ""
        self.m_rec.title = "회의 녹음 시작"
        dur = int(time.time() - (self.t0 or time.time()))
        if not self.path or not os.path.exists(self.path) or os.path.getsize(self.path) < 4000:
            self.m_state.title = "녹음이 비어 있습니다"
            rumps.alert("녹음이 비어 있습니다",
                        "시스템 설정 → 개인정보 보호 및 보안 → 마이크 에서 HEIMDALL 을 허용해주세요.")
            return
        self.busy = True
        self.m_state.title = "서버로 올리는 중…"
        self.m_state.set_callback(None)
        threading.Thread(target=self.upload, args=(self.path, self.title_text, dur),
                         daemon=True).start()

    def tick(self, _):
        if not self.t0:
            return
        s = int(time.time() - self.t0)
        self.title = f" ● {s//60}:{s%60:02d}" if s < 3600 else f" ● {s//3600}:{s%3600//60:02d}:{s%60:02d}"

    def upload(self, path, title, dur):
        try:
            token = self.auth.token()
            name = f"jobs/{int(time.time())}_{os.getpid()}.m4a"
            data = open(path, "rb").read()
            u = f"{SB_URL}/storage/v1/object/hd-audio/{name}"
            req = urllib.request.Request(u, data=data, method="POST", headers={
                "apikey": SB_KEY, "Authorization": f"Bearer {token}",
                "Content-Type": "audio/mp4", "x-upsert": "true"})
            urllib.request.urlopen(req, timeout=1800)

            me = self.auth.me() or {}
            api("hd_job", "POST", [{
                "title": title, "participants": self.parts, "audio_path": name,
                "duration_sec": dur, "device": "맥 녹음기",
                "created_name": me.get("name") or self.auth.email,
            }], token=token)

            os.remove(path)
            self.m_state.title = "올렸습니다 · 처리 대기 중"
            self.notify("업로드 완료", f"{title} — 회의록이 만들어지면 웹에서 보실 수 있습니다")
        except Exception as e:
            self.m_state.title = "업로드 실패 (파일은 보관했습니다)"
            self.notify("업로드 실패", f"{e}"[:120])
        finally:
            self.busy = False

    # 메뉴
    def set_parts(self, _):
        t = ask("참석자를 쉼표로 구분해 적어주세요  (예: 홍석진, 김윤회, 김현우)",
                "참석자", default=self.parts, ok="저장")
        if t is not None:
            self.parts = t.strip(); self._save("participants", self.parts)
            say_ok("참석자", self.parts or "비워 두었습니다.")

    def open_web(self, _=None):
        # 서버 주소(SB_URL)로는 절대 보내지 않습니다. 사람이 볼 화면이 아닙니다.
        if not WEB_URL:
            rumps.alert("웹 주소가 설정되어 있지 않습니다",
                        "관리자에게 설치 파일을 다시 받아달라고 요청해주세요.\n"
                        "자동 업데이트도 이 주소가 있어야 동작합니다.")
            return
        webbrowser.open(WEB_URL)

    def check_jobs(self, _):
        def run():
            t = self.auth.token()
            if not t:
                rumps.alert("로그인이 필요합니다"); return
            try:
                js = api("hd_job?select=title,status,created_at&order=created_at.desc&limit=5",
                         token=t) or []
            except Exception as e:
                rumps.alert("확인 실패", str(e)); return
            if not js:
                rumps.alert("올린 녹음이 없습니다"); return
            label = {"queued": "대기 중", "working": "처리 중", "done": "완료", "failed": "실패"}
            rumps.alert("최근 녹음", "\n".join(
                f"· {j['title']} — {label.get(j['status'], j['status'])}" for j in js))
        threading.Thread(target=run, daemon=True).start()

    def bye(self, _):
        if self.rec.proc or self.rec.av:
            self.stop()
        rumps.quit_application()


if __name__ == "__main__":
    if not SB_URL or not SB_KEY:
        sys.exit("설정 파일이 없습니다. 설치 프로그램을 다시 실행해주세요.")
    ensure_autostart()
    Client().run()
