#!/usr/bin/env python3
"""HEIMDALL 녹음기 — 윈도우

작업표시줄 트레이의 번개 아이콘에서 회의를 녹음해 서버로 올립니다.
전사·회의록은 처리 서버가 만듭니다. 비밀 키는 들어 있지 않습니다.
"""
import ctypes, hashlib, json, os, re, shutil, subprocess, sys, threading, time
import urllib.error, urllib.request, webbrowser

VERSION = "1.0"

HOME = os.path.dirname(os.path.abspath(__file__))
CONF_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                        "HEIMDALL_REC")
PENDING = os.path.join(CONF_DIR, "pending")
os.makedirs(PENDING, exist_ok=True)
SESSION = os.path.join(CONF_DIR, "session.json")
NOWIN = 0x08000000


def conf():
    p = os.path.join(HOME, "settings.json")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}


CONF = conf()
SB_URL = CONF.get("supabase_url", "").rstrip("/")
SB_KEY = CONF.get("supabase_anon_key", "")
WEB_URL = CONF.get("web_url", "").rstrip("/")


# ─────────────────────────────────────────── 대화 상자 (윈도우 기본)
def alert(title, msg):
    ctypes.windll.user32.MessageBoxW(0, msg, title, 0x40)


def ask_text(title, prompt, default="", secret=False):
    """작은 입력 창. tkinter 는 윈도우 기본 파이썬에 들어 있습니다."""
    import tkinter as tk
    box = {}
    root = tk.Tk()
    root.title(title)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    tk.Label(root, text=prompt, padx=16, pady=8, justify="left").pack(anchor="w")
    ent = tk.Entry(root, width=38, show="•" if secret else "")
    ent.insert(0, default)
    ent.pack(padx=16, pady=4)
    ent.focus_set()

    def done(ok):
        box["v"] = ent.get() if ok else None
        root.destroy()

    fr = tk.Frame(root); fr.pack(pady=10)
    tk.Button(fr, text="확인", width=10, command=lambda: done(True)).pack(side="left", padx=6)
    tk.Button(fr, text="취소", width=10, command=lambda: done(False)).pack(side="left", padx=6)
    root.bind("<Return>", lambda e: done(True))
    root.bind("<Escape>", lambda e: done(False))
    root.eval('tk::PlaceWindow . center')
    root.mainloop()
    return box.get("v")


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
        self.uid = ""
        if os.path.exists(SESSION):
            try:
                d = json.load(open(SESSION, encoding="utf-8"))
                self.refresh, self.email = d.get("refresh"), d.get("email", "")
                self.uid = d.get("uid", "")
                self.renew()
            except Exception:
                pass

    def save(self):
        json.dump({"refresh": self.refresh, "email": self.email, "uid": self.uid},
                  open(SESSION, "w", encoding="utf-8"))

    def login(self, email, pw):
        d = api("token?grant_type=password", "POST",
                {"email": email, "password": pw}, base="auth/v1")
        self.access, self.refresh = d["access_token"], d["refresh_token"]
        self.email = email
        self.uid = (d.get("user") or {}).get("id", "")
        self.save()

    def renew(self):
        if not self.refresh:
            return False
        try:
            d = api("token?grant_type=refresh_token", "POST",
                    {"refresh_token": self.refresh}, base="auth/v1")
            self.access, self.refresh = d["access_token"], d["refresh_token"]
            self.uid = (d.get("user") or {}).get("id", "") or self.uid
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
        self.email = self.uid = ""
        try:
            os.remove(SESSION)
        except Exception:
            pass

    def me(self):
        t = self.token()
        if not t:
            return None
        if not self.uid:
            try:
                self.uid = (api("user", token=t, base="auth/v1") or {}).get("id", "")
                self.save()
            except Exception:
                return None
        try:
            r = api(f"hd_member?id=eq.{self.uid}&select=name,approved,role,company,dept",
                    token=t)
            return r[0] if r else None
        except Exception:
            return None


# ─────────────────────────────────────────── 녹음 (ffmpeg)
def find_mic():
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-list_devices", "true",
                            "-f", "dshow", "-i", "dummy"],
                           capture_output=True, encoding="utf-8", errors="replace",
                           creationflags=NOWIN).stderr
    except FileNotFoundError:
        return None
    names = re.findall(r'"([^"]+)"\s*\(audio\)', r)
    if not names:
        prev = None
        for line in r.splitlines():
            m = re.search(r'"(.+?)"', line)
            if m and "Alternative name" not in line:
                prev = m.group(1)
            elif prev and "audio" in line.lower():
                names.append(prev); prev = None
    for n in names:
        if re.search(r"jabra|speak2", n, re.I):
            return n
    return names[0] if names else None


class Recorder:
    def __init__(self):
        self.proc = None

    def start(self, path):
        dev = find_mic()
        if not dev:
            raise RuntimeError("마이크를 찾지 못했습니다. 연결을 확인해주세요.")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "dshow", "-i", f"audio={dev}",
               "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "64k", path]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                     creationflags=NOWIN | 0x00000200)

    def stop(self):
        if not self.proc:
            return
        try:
            self.proc.stdin.write(b"q"); self.proc.stdin.flush()
            self.proc.wait(timeout=15)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None


# ─────────────────────────────────────────── 보관함 (못 올린 녹음)
def meta_path(a):
    return os.path.join(PENDING, os.path.basename(a) + ".json")


def save_pending(a, title, parts, dur=0):
    json.dump({"audio": a, "title": title, "participants": parts, "duration": dur},
              open(meta_path(a), "w", encoding="utf-8"), ensure_ascii=False)


def clear_pending(a):
    for p in (a, meta_path(a)):
        try:
            os.remove(p)
        except Exception:
            pass


def list_pending():
    out = []
    for n in sorted(os.listdir(PENDING)):
        if not n.endswith(".json"):
            continue
        try:
            m = json.load(open(os.path.join(PENDING, n), encoding="utf-8"))
        except Exception:
            continue
        a = m.get("audio", "")
        if a and os.path.exists(a) and os.path.getsize(a) > 4000:
            out.append(m)
        elif a and not os.path.exists(a):
            try:
                os.remove(os.path.join(PENDING, n))
            except Exception:
                pass
    return out


# ─────────────────────────────────────────── 스스로 갱신
def self_update():
    if not WEB_URL:
        return
    try:
        with urllib.request.urlopen(
                f"{WEB_URL}/winclient-version.json?t={int(time.time())}", timeout=20) as r:
            info = json.load(r)
    except Exception:
        return

    def vnum(v):
        try:
            return tuple(int(x) for x in str(v).split("."))
        except Exception:
            return (0,)

    if vnum(info.get("version")) <= vnum(VERSION):
        return
    url = str(info.get("url", ""))
    if not url.startswith(WEB_URL):
        return
    try:
        with urllib.request.urlopen(url + f"?t={int(time.time())}", timeout=60) as r:
            data = r.read()
        if (info.get("sha256") or "").lower() != hashlib.sha256(data).hexdigest():
            return
        if b"class App" not in data:
            return
        target = os.path.join(HOME, "winclient.py")
        shutil.copy2(target, target + ".bak")
        open(target, "wb").write(data)
        subprocess.Popen([sys.executable, target], creationflags=NOWIN)
        os._exit(0)
    except Exception:
        pass


# ─────────────────────────────────────────── 트레이 앱
class App:
    def __init__(self):
        import pystray
        from PIL import Image, ImageDraw
        self.auth = Auth()
        self.rec = Recorder()
        self.path = None
        self.t0 = None
        self.title = ""
        self.busy = False
        self.state = "준비 중"
        self.parts = self._load("participants")

        S = 64
        im = Image.new("RGBA", (S, S), (27, 31, 38, 255))
        d = ImageDraw.Draw(im)
        POLY = [(0, 37.5), (236, 0), (236, 50), (400, 50), (400, 62.5),
                (164, 100), (164, 50), (0, 50)]
        sc = (S * 0.78) / 400
        ox = S * 0.11; oy = (S - 100 * sc) / 2
        d.polygon([(ox + x * sc, oy + y * sc) for x, y in POLY], fill=(255, 255, 255, 255))
        im_rec = im.copy()
        dr = ImageDraw.Draw(im_rec)
        dr.ellipse([S - 26, 4, S - 4, 26], fill=(220, 60, 60, 255))
        self.icon_idle, self.icon_rec = im, im_rec

        self.pystray = pystray
        self.icon = pystray.Icon("heimdall-rec", im, "HEIMDALL 녹음기", self.menu())

    # 작은 저장소
    def _p(self, k):
        return os.path.join(CONF_DIR, k)

    def _load(self, k):
        try:
            return open(self._p(k), encoding="utf-8").read().strip()
        except Exception:
            return ""

    def _save(self, k, v):
        open(self._p(k), "w", encoding="utf-8").write(v)

    def menu(self):
        P = self.pystray
        rec_label = "회의 녹음 종료" if self.rec.proc else "회의 녹음 시작"
        who = self.auth.email or "로그인 안 됨"
        return P.Menu(
            P.MenuItem(rec_label, self.toggle, default=True),
            P.MenuItem(lambda item: self.state, None, enabled=False),
            P.Menu.SEPARATOR,
            P.MenuItem("회의록 보러 가기", lambda: webbrowser.open(WEB_URL or SB_URL)),
            P.MenuItem("참석자 설정…", self.set_parts),
            P.MenuItem(f"{who} · " + ("로그아웃" if self.auth.refresh else "로그인"),
                       self.login),
            P.Menu.SEPARATOR,
            P.MenuItem(f"버전 {VERSION}", None, enabled=False),
            P.MenuItem("종료", self.quit),
        )

    def refresh(self):
        self.icon.menu = self.menu()
        self.icon.icon = self.icon_rec if self.rec.proc else self.icon_idle
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def notify(self, msg):
        try:
            self.icon.notify(msg, "HEIMDALL")
        except Exception:
            pass

    # 로그인
    def login(self, *_):
        if self.auth.refresh:
            self.auth.logout()
            self.state = "로그인이 필요합니다"
            self.refresh()
            return
        email = ask_text("HEIMDALL 로그인", "웹에서 가입하신 이메일", self._load("email"))
        if not email:
            return
        pw = ask_text("HEIMDALL 로그인", f"{email}\n비밀번호", secret=True)
        if not pw:
            return
        try:
            self.auth.login(email.strip(), pw)
        except urllib.error.HTTPError:
            alert("로그인 실패", "이메일 또는 비밀번호가 맞지 않습니다.\n"
                               "웹에서 먼저 가입하셨는지 확인해주세요.")
            return
        except Exception as e:
            alert("로그인 실패", str(e))
            return
        self._save("email", email.strip())
        me = self.auth.me() or {}
        if me.get("approved"):
            self.state = "대기 중"
            alert("로그인했습니다", f"{me.get('name') or email} 님, 이제 녹음하실 수 있습니다.")
        else:
            self.state = "가입 승인 대기 중"
            alert("가입 승인 대기", "관리자가 승인하면 녹음하실 수 있습니다.")
        self.refresh()

    def set_parts(self, *_):
        t = ask_text("참석자", "참석자를 쉼표로 구분해 적어주세요\n예: 홍석진, 김윤회, 김현우",
                     self.parts)
        if t is not None:
            self.parts = t.strip()
            self._save("participants", self.parts)

    # 녹음
    def toggle(self, *_):
        if self.busy:
            alert("잠시만요", "이전 녹음을 올리는 중입니다.")
            return
        if self.rec.proc:
            self.stop()
        else:
            self.start()

    def start(self):
        if not self.auth.token():
            alert("로그인이 필요합니다", "번개 아이콘 메뉴에서 로그인해주세요.")
            return
        default = time.strftime("%y%m%d") + " 회의"
        t = ask_text("HEIMDALL", "회의명을 적어주세요", default)
        if t is None:
            return
        self.title = t.strip() or default
        self.path = os.path.join(CONF_DIR, f"rec_{int(time.time())}.m4a")
        try:
            self.rec.start(self.path)
        except Exception as e:
            alert("녹음을 시작하지 못했습니다", str(e))
            return
        save_pending(self.path, self.title, self.parts)
        self.t0 = time.time()
        self.state = f"녹음 중 · {self.title}"
        self.refresh()
        self.notify(f"녹음을 시작했습니다 · {self.title}")

    def stop(self):
        self.rec.stop()
        dur = int(time.time() - (self.t0 or time.time()))
        self.refresh()
        if not self.path or not os.path.exists(self.path) or os.path.getsize(self.path) < 4000:
            self.state = "녹음이 비어 있습니다 — 마이크 확인"
            self.refresh()
            alert("녹음이 비어 있습니다",
                  "마이크가 연결되어 있는지, 윈도우 설정 → 개인 정보 → 마이크에서\n"
                  "앱의 마이크 사용이 켜져 있는지 확인해주세요.")
            return
        save_pending(self.path, self.title, self.parts, dur)
        self.busy = True
        self.state = "서버로 올리는 중…"
        self.refresh()
        threading.Thread(target=self._upload_all, daemon=True).start()

    def upload_one(self, m):
        token = self.auth.token()
        if not token:
            raise RuntimeError("로그인이 필요합니다")
        path = m["audio"]
        name = f"jobs/{int(time.time())}_{os.getpid()}_{os.path.basename(path)}"
        data = open(path, "rb").read()
        req = urllib.request.Request(
            f"{SB_URL}/storage/v1/object/hd-audio/{name}", data=data, method="POST",
            headers={"apikey": SB_KEY, "Authorization": f"Bearer {token}",
                     "Content-Type": "audio/mp4", "x-upsert": "true"})
        urllib.request.urlopen(req, timeout=1800)
        me = self.auth.me() or {}
        api("hd_job", "POST", [{
            "title": m.get("title") or "회의",
            "participants": m.get("participants", ""),
            "audio_path": name, "duration_sec": int(m.get("duration") or 0),
            "device": "윈도우 녹음기",
            "created_by": self.auth.uid,
            "created_name": me.get("name") or self.auth.email,
            "company": me.get("company", ""), "dept": me.get("dept", ""),
        }], token=token)
        clear_pending(path)

    def _upload_all(self):
        try:
            items = list_pending()
            done = 0
            for m in items:
                try:
                    self.upload_one(m)
                    done += 1
                except Exception:
                    break
            if done:
                self.state = "올렸습니다 · 처리 대기 중"
                self.notify("업로드 완료 — 잠시 뒤 웹에서 회의록을 보실 수 있습니다")
            else:
                self.state = "올리지 못했습니다 — 나중에 다시 시도합니다"
        finally:
            self.busy = False
            self.refresh()

    def resume_loop(self):
        while True:
            time.sleep(300)
            if not self.busy and not self.rec.proc and list_pending() and self.auth.token():
                self.busy = True
                self._upload_all()

    def update_loop(self):
        while True:
            time.sleep(6 * 3600)
            if not self.rec.proc and not self.busy:
                self_update()

    def quit(self, *_):
        if self.rec.proc:
            self.rec.stop()
            dur = int(time.time() - (self.t0 or time.time()))
            if self.path and os.path.exists(self.path) and os.path.getsize(self.path) > 4000:
                save_pending(self.path, self.title, self.parts, dur)
                alert("녹음을 저장했습니다",
                      f"「{self.title}」 녹음이 보관되었습니다.\n다시 켜지면 자동으로 올라갑니다.")
        self.icon.stop()
        os._exit(0)

    def run(self):
        me = self.auth.me()
        if not me:
            self.state = "로그인이 필요합니다"
        elif not me.get("approved"):
            self.state = "가입 승인 대기 중"
        else:
            self.state = "대기 중"
        threading.Thread(target=self.resume_loop, daemon=True).start()
        threading.Thread(target=self.update_loop, daemon=True).start()
        threading.Thread(target=lambda: (time.sleep(3), self._upload_all())
                         if list_pending() and self.auth.token() else None,
                         daemon=True).start()
        self.refresh()
        self.icon.run()


if __name__ == "__main__":
    if not SB_URL or not SB_KEY:
        alert("HEIMDALL", "설정 파일이 없습니다. 설치 프로그램을 다시 실행해주세요.")
        sys.exit(1)
    self_update()
    App().run()
