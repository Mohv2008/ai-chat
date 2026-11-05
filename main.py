"""AI Chat: a Fenn notch plugin.

Chat with an AI from the notch. In Settings the user picks a provider and either
pastes their own API key (OpenAI, Anthropic, Gemini, any OpenAI-compatible
service) or points at a local model (Ollama, LM Studio).

Python 3.8+, standard library only. Protocol: https://fenn.click/docs/messages
"""

import base64
import html
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
import zlib

# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
# fmt: "openai" = /chat/completions (also Ollama, LM Studio, OpenRouter, Groq...)
#      "anthropic" = /v1/messages      "gemini" = :streamGenerateContent

PRESETS = [
    {"id": "openai", "cloud": True, "label": "OpenAI", "fmt": "openai", "key": True, "key_required": True,
     "base": "https://api.openai.com/v1", "model": "gpt-4o-mini", "edit_base": False,
     "help": "Paste an API key from platform.openai.com."},
    {"id": "anthropic", "cloud": True, "label": "Anthropic", "fmt": "anthropic", "key": True, "key_required": True,
     "base": "https://api.anthropic.com", "model": "claude-sonnet-5-5", "edit_base": False,
     "help": "Paste an API key from console.anthropic.com."},
    {"id": "gemini", "cloud": True, "label": "Gemini", "fmt": "gemini", "key": True, "key_required": True,
     "base": "https://generativelanguage.googleapis.com/v1beta", "model": "gemini-2.5-flash",
     "edit_base": False, "help": "Paste an API key from aistudio.google.com."},
    {"id": "ollama", "label": "Ollama", "fmt": "openai", "key": False, "key_required": False,
     "base": "http://localhost:11434/v1", "model": "llama3.2", "edit_base": True,
     "help": "Local and free. Run: ollama pull llama3.2, then Detect."},
    {"id": "lmstudio", "label": "LM Studio", "fmt": "openai", "key": False, "key_required": False,
     "base": "http://localhost:1234/v1", "model": "local-model", "edit_base": True,
     "help": "Local and free. Start the LM Studio server, then Detect."},
    {"id": "custom", "label": "Custom", "fmt": "openai", "key": True, "key_required": False,
     "base": "https://openrouter.ai/api/v1", "model": "", "edit_base": True,
     "help": "Any OpenAI-compatible URL (OpenRouter, Groq...). Key optional."},
]
PRESET = {p["id"]: p for p in PRESETS}

SYSTEM_PROMPT = (
    "You are a helpful assistant inside a small notch panel on Windows. Be concise. "
    "Reply in plain text: no markdown headings, tables or bold unless the user asks."
)
MAX_HISTORY_SENT = 20     # messages sent to the model
MAX_HISTORY_SAVED = 60    # messages kept on disk
MAX_SHOWN = 8             # messages drawn in the page
MAX_REPLY_CHARS = 400_000  # stop reading a reply that never ends
MAX_MODELS_SHOWN = 100    # models listed after "Detect models"

# Attachments
MAX_ATTACH = 5                     # files per message
MB = 1024 * 1024
MAX_FILE_BYTES = {"pdf": 10 * MB, "image": 5 * MB, "docx": 20 * MB, "text": 2 * MB}
MAX_TOTAL_BYTES = 20 * MB          # all files staged for one message
TEXT_CAP_CLOUD = 200_000           # chars of extracted text per message
TEXT_CAP_LOCAL = 30_000            # local models usually have small context windows
DEFAULT_FILE_PROMPT = "Please look at the attached file(s)."
IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".gif": "image/gif", ".webp": "image/webp"}
TEXT_EXT = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".xml", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".log", ".html", ".htm", ".css", ".scss", ".js", ".mjs",
    ".ts", ".jsx", ".tsx", ".vue", ".py", ".pyw", ".java", ".kt", ".c", ".h", ".cpp", ".hpp",
    ".cc", ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".sh", ".bat", ".cmd", ".ps1", ".sql",
    ".lua", ".r", ".tex", ".gradle", ".env", ".gitignore", ".dockerfile",
}
KIND_ICON = {"pdf": "file-pdf", "docx": "file-doc", "image": "image", "text": "file-text"}

# ---------------------------------------------------------------------------
# Talking to Fenn
# ---------------------------------------------------------------------------

DATA_DIR = (os.environ.get("FENN_DATA_DIR") or os.environ.get("NOOK_DATA_DIR")
            or os.path.join(os.getcwd(), ".data"))
os.makedirs(DATA_DIR, exist_ok=True)

_out_lock = threading.Lock()


def emit(message):
    line = json.dumps(message, separators=(",", ":"))  # ASCII-only on the wire
    with _out_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def log(message, level="info"):
    emit({"type": "log", "level": level, "message": str(message)})


def node(type_, **kw):
    d = {"type": type_}
    d.update({k: v for k, v in kw.items() if v is not None})
    return d


def text(s, size=None, color=None, bold=None, wrap=None):
    return node("text", text=str(s), size=size, color=color, bold=bold, wrap=wrap)


def icon(name, size=None, color=None):
    return node("icon", icon=name, size=size, color=color)


def column(children, **kw):
    return node("column", children=children, **kw)


def row(children, **kw):
    return node("row", children=children, **kw)


def card(children, **kw):
    return node("card", children=children, **kw)


def button(id_, label=None, icon_name=None, primary=None, width=None, tooltip=None):
    return node("button", id=id_, label=label, icon=icon_name, primary=primary,
                width=width, tooltip=tooltip)


def text_input(id_, value="", hint=None, width=None, live=True):
    return node("input", id=id_, value=value, hint=hint, width=width, live=live)


# ---------------------------------------------------------------------------
# Storage (settings + history). API keys are encrypted with Windows DPAPI.
# ---------------------------------------------------------------------------

def load_json(name, default):
    try:
        with open(os.path.join(DATA_DIR, name + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(name, value):
    path = os.path.join(DATA_DIR, name + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _dpapi(data, encrypt):
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if encrypt else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def seal(secret):
    """Returns (scheme, base64). Encrypted to this Windows user when possible."""
    if not secret:
        return "", ""
    raw = secret.encode("utf-8")
    if sys.platform == "win32":
        try:
            return "dpapi", base64.b64encode(_dpapi(raw, True)).decode()
        except Exception as e:
            log(f"DPAPI encryption failed, key not saved: {e}", "error")
            raise ChatError("Windows couldn't encrypt your API key, so it was not saved.")
    return "plain", base64.b64encode(raw).decode()      # non-Windows (development) only


def unseal(scheme, blob):
    if not blob:
        return ""
    try:
        raw = base64.b64decode(blob)
        if scheme == "dpapi":
            raw = _dpapi(raw, False)
        return raw.decode("utf-8")
    except Exception:
        return ""


settings = load_json("settings", {})
settings.setdefault("active", "")
settings.setdefault("profiles", {})
messages = load_json("history", [])
if not isinstance(messages, list):
    messages = []


def _clean_history(items):
    """history.json is just a file on disk: only keep well-formed messages, and
    only attachment records that can't point outside the attachments folder."""
    ok_kinds = {"pdf", "docx", "image", "text"}
    ok_mimes = {"image/png", "image/jpeg", "image/gif", "image/webp"}
    out = []
    for m in items:
        if not (isinstance(m, dict) and m.get("role") in ("user", "assistant")
                and isinstance(m.get("content"), str)):
            continue
        clean = {"role": m["role"], "content": m["content"]}
        files = []
        for f in m.get("files") or []:
            if (isinstance(f, dict) and isinstance(f.get("id"), str) and re.fullmatch(r"[0-9a-f]{12}", f["id"])
                    and isinstance(f.get("ext", ""), str) and re.fullmatch(r"(\.[a-z0-9]{1,9})?", f.get("ext", ""))
                    and f.get("kind") in ok_kinds and isinstance(f.get("name"), str)
                    and isinstance(f.get("size"), int)
                    and (f["kind"] != "image" or f.get("mime") in ok_mimes)):
                files.append({k: f[k] for k in ("id", "ext", "kind", "name", "size", "mime") if k in f})
        if files:
            clean["files"] = files
        out.append(clean)
    return out


messages = _clean_history(messages)


def migrate_plain_keys():
    """Keys saved unencrypted (fallback of older versions) get encrypted on Windows."""
    if sys.platform != "win32":
        return
    changed = False
    for entry in settings["profiles"].values():
        if isinstance(entry, dict) and entry.get("key_scheme") == "plain" and entry.get("key_enc"):
            try:
                entry["key_scheme"], entry["key_enc"] = seal(unseal("plain", entry["key_enc"]))
                changed = True
            except Exception:
                pass
    if changed:
        try:
            save_json("settings", settings)
        except OSError:
            pass


def profile(pid):
    pre = PRESET[pid]
    p = settings["profiles"].get(pid, {})
    return {
        "pid": pid,
        "model": p.get("model", pre["model"]),
        "base": p.get("base_url", pre["base"]),
        "key": unseal(p.get("key_scheme"), p.get("key_enc", "")),
    }


def active_pid():
    pid = settings.get("active")
    return pid if pid in PRESET else ""


def is_configured():
    pid = active_pid()
    if not pid:
        return False
    pre, prof = PRESET[pid], profile(pid)
    return bool(prof["model"] and prof["base"] and (prof["key"] or not pre["key_required"]))


# ---------------------------------------------------------------------------
# Provider calls (all stream; stdlib urllib only)
# ---------------------------------------------------------------------------

class ChatError(Exception):
    pass


def _http_error_text(e):
    try:
        body = e.read(4000).decode("utf-8", "replace")
        data = json.loads(body)
        err = data.get("error", data)
        if isinstance(err, dict):
            err = err.get("message") or err
        if isinstance(data, list) and data:
            err = (data[0].get("error") or {}).get("message", body)
        msg = str(err)
    except Exception:
        msg = e.reason if isinstance(getattr(e, "reason", None), str) else ""
    hint = ""
    if e.code in (401, 403):
        hint = " (check your API key)"
    elif e.code == 404:
        hint = " (check the model name and URL)"
    elif e.code == 429:
        hint = " (rate limited or out of credit)"
    return f"{e.code}: {msg[:300]}{hint}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would copy the Authorization / API-key headers to whatever host a
    redirect points at. Refuse redirects instead of leaking the key."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ChatError(f"The server tried to redirect to {newurl.split('?')[0][:80]}. "
                        "Not following it, to keep your key safe. Check the URL in Settings.")


_OPENER = urllib.request.build_opener(_NoRedirect)
_KEY_HEADERS = {"authorization", "x-api-key", "x-goog-api-key"}


def _is_local_host(host):
    if host.lower() == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def _check_url(url, headers):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ChatError("The server URL must start with http:// or https://")
    if parts.username or parts.password:
        raise ChatError("Don't put a username or password inside the URL.")
    sends_key = any(k.lower() in _KEY_HEADERS and v for k, v in headers.items())
    if sends_key and parts.scheme == "http" and not _is_local_host(parts.hostname):
        raise ChatError("Your API key would travel unencrypted over http://. "
                        "Use an https:// URL (http:// is only allowed for this PC or your home network).")


def _open(url, headers, body=None, timeout=300):
    _check_url(url, headers)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        return _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise ChatError(_http_error_text(e))
    except urllib.error.URLError as e:
        host = url.split("/")[2] if "//" in url else url
        raise ChatError(f"Can't reach {host}: {e.reason}. Is the server running?")


def _sse(resp, cancelled):
    for raw in resp:
        if cancelled():
            return
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except ValueError:
            continue


def _err_in_event(ev):
    err = ev.get("error") if isinstance(ev, dict) else None
    if err:
        raise ChatError(str(err.get("message", err)) if isinstance(err, dict) else str(err))


# ---------------------------------------------------------------------------
# Attachments: staging, text extraction, per-provider payloads
# ---------------------------------------------------------------------------
# Files are copied into DATA_DIR/attachments when picked, so they keep working
# if the original moves. A message stores only small metadata ("files"); the
# payload is rebuilt from the copy on every request, for whichever provider is
# active at that moment.

ATTACH_DIR = os.path.join(DATA_DIR, "attachments")
os.makedirs(ATTACH_DIR, exist_ok=True)

pending = []            # staged files shown as chips above the input
attach_error = ""
picking = False
_text_cache = {}        # attachment id -> extracted text


def _ext(name):
    base = os.path.basename(name).lower()
    ext = os.path.splitext(base)[1]
    return ext or base      # ".gitignore" / "dockerfile" count as their own extension


def _looks_like_text(path):
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
    except OSError:
        return False
    if chunk.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True         # UTF-16 with BOM (common from Windows tools)
    return b"\0" not in chunk


def detect_kind(path):
    ext = _ext(path)
    if ext == ".pdf":
        return "pdf"
    if ext == ".docx":
        return "docx"
    if ext in IMAGE_MIME:
        return "image"
    if ext == ".doc":
        raise ChatError("Old .doc files aren't supported. Save it as .docx first.")
    if ext in TEXT_EXT or (ext not in (".exe", ".zip", ".7z", ".rar", ".dll") and _looks_like_text(path)):
        return "text"
    raise ChatError(f"Can't attach {os.path.basename(path)}: unsupported file type.")


def attachment_path(rec):
    # ids/extensions are checked again here, so a record can never point outside ATTACH_DIR
    if not re.fullmatch(r"[0-9a-f]{12}", str(rec.get("id", ""))) or \
            not re.fullmatch(r"(\.[a-z0-9]{1,9})?", str(rec.get("ext", ""))):
        raise ChatError("Invalid attachment record.")
    return os.path.join(ATTACH_DIR, rec["id"] + rec.get("ext", ""))


def short_name(name, limit):
    if len(name) <= limit:
        return name
    stem, ext = os.path.splitext(name)
    keep = max(1, limit - len(ext) - 1)
    return stem[:keep] + "\u2026" + ext


def stage_file(path):
    """Validate + copy one file. Returns the metadata record, or raises ChatError."""
    path = os.path.abspath(path)
    name = os.path.basename(path)
    if not os.path.isfile(path):
        raise ChatError(f"{name}: not a file.")
    kind = detect_kind(path)
    size = os.path.getsize(path)
    if size == 0:
        raise ChatError(f"{name} is empty.")
    limit = MAX_FILE_BYTES[kind]
    if size > limit:
        raise ChatError(f"{name} is too big ({size / MB:.1f} MB; the limit for this type is {limit // MB} MB).")
    if sum(f["size"] for f in pending) + size > MAX_TOTAL_BYTES:
        raise ChatError(f"Attachments are limited to {MAX_TOTAL_BYTES // MB} MB per message.")
    ext = os.path.splitext(name)[1].lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,9}", ext):
        ext = ""                                   # odd characters: store without an extension
    rec = {"id": uuid.uuid4().hex[:12], "name": name, "kind": kind, "size": size, "ext": ext}
    if kind == "image":
        rec["mime"] = IMAGE_MIME[os.path.splitext(name)[1].lower()]
    shutil.copyfile(path, attachment_path(rec))
    return rec


def add_files(paths):
    """Stage files for the next message. Problems are shown, not raised."""
    global attach_error
    errors = []
    with lock:
        for p in paths:
            if len(pending) >= MAX_ATTACH:
                errors.append(f"Up to {MAX_ATTACH} files per message.")
                break
            try:
                pending.append(stage_file(p))
            except ChatError as e:
                errors.append(str(e))
            except OSError as e:
                errors.append(f"{os.path.basename(str(p))}: {e.strerror or e}")
    attach_error = " ".join(errors)[:240]


def remove_pending(index):
    global attach_error
    with lock:
        if 0 <= index < len(pending):
            rec = pending.pop(index)
            _text_cache.pop(rec["id"], None)
            try:
                os.remove(attachment_path(rec))
            except OSError:
                pass
    attach_error = ""


def gc_attachments():
    """Delete stored copies that no message or chip refers to any more."""
    with lock:
        keep = {f["id"] for m in messages for f in (m.get("files") or [])}
        keep |= {f["id"] for f in pending}
        try:
            names = os.listdir(ATTACH_DIR)
        except OSError:
            return
        for n in names:
            if os.path.splitext(n)[0] not in keep:
                try:
                    os.remove(os.path.join(ATTACH_DIR, n))
                except OSError:
                    pass
        for k in list(_text_cache):
            if k not in keep:
                del _text_cache[k]


# --- picking files -----------------------------------------------------------

def _dialog_patterns():
    exts = sorted({".pdf", ".docx"} | set(IMAGE_MIME) | {e for e in TEXT_EXT if e.startswith(".") and len(e) > 1})
    return " ".join("*" + e for e in exts)


def _pick_tk():
    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    try:
        chosen = filedialog.askopenfilenames(
            parent=root, title="Attach files",
            filetypes=[("PDF, images, Word, text and code", _dialog_patterns()), ("All files", "*.*")])
        if isinstance(chosen, str):
            chosen = root.tk.splitlist(chosen)
        return [str(p) for p in chosen]
    finally:
        root.destroy()


def _pick_powershell():
    if sys.platform != "win32":
        return []
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$o=New-Object System.Windows.Forms.Form;$o.TopMost=$true;"
        "$d=New-Object System.Windows.Forms.OpenFileDialog;"
        "$d.Multiselect=$true;$d.Title='Attach files';"
        "if($d.ShowDialog($o) -eq 'OK'){$d.FileNames}"
    )
    done = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          creationflags=0x08000000, timeout=900)   # CREATE_NO_WINDOW
    return [ln.strip() for ln in done.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]


def pick_files_dialog():
    """Native file picker. Tkinter first (ships with python.org Python), PowerShell second."""
    try:
        return _pick_tk()
    except Exception as e:
        log(f"tkinter picker unavailable ({type(e).__name__}), trying PowerShell", "warn")
    return _pick_powershell()


def open_picker():
    global picking, attach_error
    if picking:
        return
    picking = True

    def work():
        global picking, attach_error
        try:
            paths = pick_files_dialog()
            if paths:
                add_files(paths)
        except Exception as e:
            attach_error = f"Couldn't open the file picker: {e}"
            log(attach_error, "error")
        finally:
            picking = False
            render_page()

    threading.Thread(target=work, daemon=True).start()


def paths_from_drop(msg):
    """Pull file paths out of a drop message (accepts strings or {"path": ...} items)."""
    raw = msg.get("paths") or msg.get("files") or msg.get("items") or []
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("path") or item.get("name")
        if isinstance(item, str) and item:
            out.append(item)
    return out


# --- reading files -----------------------------------------------------------

def read_text_file(path):
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")


def docx_text(path):
    try:
        with zipfile.ZipFile(path) as z:
            with z.open("word/document.xml") as f:
                raw = f.read(60 * MB + 1)             # bounded: a zip header can lie about its size
            if len(raw) > 60 * MB:
                raise ChatError("That Word file is too large to read.")
            xml = raw.decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError):
        raise ChatError("Couldn't read that .docx (is it a real Word document?).")
    paras = []
    for chunk in xml.split("</w:p>"):
        chunk = re.sub(r"<w:tab\s*/>", "\t", chunk)
        chunk = re.sub(r"<w:(?:br|cr)\b[^>]*/>", "\n", chunk)
        runs = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>|(\t|\n)", chunk, flags=re.S)
        line = "".join(html.unescape(t) if t else sep for t, sep in runs)
        paras.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(paras)).strip()


PDF_STREAM_MAX = 8 * MB      # decompressed bytes we are willing to scan per stream
PDF_TEXT_MAX = 2_000_000     # characters the basic reader will collect
PDF_TIME_MAX = 20            # seconds the basic reader may run

_PDF_STR = re.compile(rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>|\[|\]|T\*|Td|TD|Tj|TJ|ET|BT|'|\"|-?\d*\.?\d+", re.S)
_PDF_ESC = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}


def _pdf_literal(tok):
    body, out, i = tok[1:-1], bytearray(), 0
    while i < len(body):
        c = body[i]
        if c == 0x5C and i + 1 < len(body):          # backslash
            i += 1
            n = body[i]
            if 0x30 <= n <= 0x37:                      # octal \ddd
                j = i
                while j < len(body) and j < i + 3 and 0x30 <= body[j] <= 0x37:
                    j += 1
                out.append(int(body[i:j], 8) & 0xFF)
                i = j
                continue
            if n in (10, 13):                          # line continuation
                i += 1
                continue
            out.append(_PDF_ESC.get(n, n))
        else:
            out.append(c)
        i += 1
    return bytes(out).decode("cp1252", "replace")


def _pdf_page_text(data):
    out, strings, in_arr = [], [], False
    for m in _PDF_STR.finditer(data):
        tok = m.group(0)
        if tok.startswith(b"("):
            strings.append(_pdf_literal(tok))
        elif tok.startswith(b"<"):
            hx = re.sub(rb"\s", b"", tok[1:-1])
            if len(hx) % 2 == 0 and hx:
                try:
                    strings.append(bytes.fromhex(hx.decode()).decode("cp1252", "replace"))
                except ValueError:
                    pass
        elif tok == b"[":
            in_arr = True
        elif tok == b"]":
            in_arr = False
        elif tok in (b"Tj", b"TJ", b"'", b'"'):
            out.append("".join(strings))
            strings = []
        elif tok in (b"Td", b"TD", b"T*", b"ET"):
            if strings:
                out.append("".join(strings))
                strings = []
            out.append("\n")
        elif in_arr and tok[:1] in b"-0123456789.":
            try:
                if float(tok) < -200:                  # big kerning gap = word space
                    strings.append(" ")
            except ValueError:
                pass
    return "".join(out)


def _pdf_text_basic(data):
    """Dependency-free fallback: handles plain/Flate content streams with simple fonts."""
    pages, total, t0 = [], 0, time.monotonic()
    for m in re.finditer(rb"(?<!end)stream\r?\n(.*?)\r?\n?endstream", data, re.S):
        if total > PDF_TEXT_MAX or time.monotonic() - t0 > PDF_TIME_MAX:
            break
        raw = m.group(1)
        head = data[max(0, m.start() - 400):m.start()]
        head = head[head.rfind(b"obj") + 3:] if b"obj" in head else head
        try:
            if b"ASCII85Decode" in head:               # e.g. PDFs made by reportlab
                raw = base64.a85decode(re.sub(rb"~>\s*$", b"", raw.strip()))
            if b"FlateDecode" in head:
                raw = zlib.decompressobj().decompress(raw, PDF_STREAM_MAX)   # bounded: no zip bombs
        except (ValueError, zlib.error):
            continue
        if len(raw) <= PDF_STREAM_MAX and b"BT" in raw and b"ET" in raw:
            txt = _pdf_page_text(raw)
            if txt.strip():
                pages.append(txt)
                total += len(txt)
    return "\n\n".join(pages)


def _pdf_text_pypdf(path):
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            return None
    reader = PdfReader(path)
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:
            raise ChatError("That PDF is password-protected.")
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


def _readable(txt):
    if not txt or len(txt.strip()) < 20:
        return False
    good = sum(c.isalnum() or c.isspace() or c in ".,;:!?()-'\"%$/&@" for c in txt)
    return good / len(txt) > 0.85


def pdf_text(path, name):
    txt = None
    try:
        txt = _pdf_text_pypdf(path)
    except ChatError:
        raise
    except Exception as e:
        log(f"pypdf failed on {name}: {e}", "warn")
    if not _readable(txt):
        with open(path, "rb") as f:
            txt = _pdf_text_basic(f.read())
    if not _readable(txt):
        raise ChatError(
            f"Couldn't read any text from {name} (scanned or unusual PDF). "
            "Install pypdf (pip install pypdf) for better results, or use a cloud model that reads PDFs directly.")
    return txt.strip()


def attachment_text(rec):
    """Extracted text of a text/docx/pdf attachment (cached)."""
    if rec["id"] in _text_cache:
        return _text_cache[rec["id"]]
    path = attachment_path(rec)
    if rec["kind"] == "docx":
        txt = docx_text(path)
    elif rec["kind"] == "pdf":
        txt = pdf_text(path, rec["name"])
    else:
        txt = read_text_file(path)
    if rec["kind"] in ("docx", "text") and not txt.strip():
        raise ChatError(f"{rec['name']} has no readable text.")
    _text_cache[rec["id"]] = txt
    return txt


# --- building provider payloads ---------------------------------------------
# A stored message becomes neutral parts: ("text", str) | ("image", mime, b64)
# | ("pdf", name, b64). Each provider format maps those to its own JSON.

def parts_of(m):
    return m["parts"] if "parts" in m else [("text", m.get("content", ""))]


def wire_message(m, prof):
    files = m.get("files") or []
    if not files:
        return m
    pre = PRESET[prof["pid"]]
    budget = TEXT_CAP_CLOUD if pre.get("cloud") else TEXT_CAP_LOCAL
    parts = []
    for rec in files:
        path = attachment_path(rec)
        if not os.path.isfile(path):
            parts.append(("text", f"[Attached file {rec['name']} is no longer available.]"))
            continue
        if rec["kind"] == "image":
            with open(path, "rb") as f:
                parts.append(("image", rec["mime"], base64.b64encode(f.read()).decode()))
        elif rec["kind"] == "pdf" and pre.get("cloud"):
            with open(path, "rb") as f:
                parts.append(("pdf", rec["name"], base64.b64encode(f.read()).decode()))
        else:
            body = attachment_text(rec)
            if budget <= 0:
                body = "[omitted: too much attached text for one message]"
            elif len(body) > budget:
                body = body[:budget] + f"\n[\u2026truncated, {len(body) - budget:,} more characters not shown]"
                budget = 0
            else:
                budget -= len(body)
            parts.append(("text", f"[Attached file: {rec['name']}]\n{body}\n[End of {rec['name']}]"))
    parts.append(("text", m.get("content", "")))
    return {"role": m["role"], "parts": parts}


def openai_msg(m):
    parts = [p for p in parts_of(m) if not (p[0] == "text" and not p[1])] or [("text", "")]
    if all(p[0] == "text" for p in parts):
        return {"role": m["role"], "content": "\n\n".join(p[1] for p in parts)}
    out = []
    for p in parts:
        if p[0] == "text":
            out.append({"type": "text", "text": p[1]})
        elif p[0] == "image":
            out.append({"type": "image_url", "image_url": {"url": f"data:{p[1]};base64,{p[2]}"}})
        else:
            out.append({"type": "file", "file": {"filename": p[1],
                                                 "file_data": "data:application/pdf;base64," + p[2]}})
    return {"role": m["role"], "content": out}


def anthropic_msg(m):
    parts = [p for p in parts_of(m) if not (p[0] == "text" and not p[1])] or [("text", " ")]
    if all(p[0] == "text" for p in parts):
        return {"role": m["role"], "content": "\n\n".join(p[1] for p in parts)}
    out = []
    for p in parts:
        if p[0] == "text":
            out.append({"type": "text", "text": p[1]})
        elif p[0] == "image":
            out.append({"type": "image", "source": {"type": "base64", "media_type": p[1], "data": p[2]}})
        else:
            out.append({"type": "document", "title": p[1],
                        "source": {"type": "base64", "media_type": "application/pdf", "data": p[2]}})
    return {"role": m["role"], "content": out}


def gemini_msg(m):
    out = []
    for p in parts_of(m):
        if p[0] == "text":
            if p[1]:
                out.append({"text": p[1]})
        elif p[0] == "image":
            out.append({"inlineData": {"mimeType": p[1], "data": p[2]}})
        else:
            out.append({"inlineData": {"mimeType": "application/pdf", "data": p[2]}})
    return {"role": "model" if m["role"] == "assistant" else "user", "parts": out or [{"text": " "}]}


def stream_openai(prof, msgs, on_delta, cancelled, holder):
    headers = {"Authorization": "Bearer " + prof["key"]} if prof["key"] else {}
    body = {"model": prof["model"], "stream": True,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + [openai_msg(m) for m in msgs]}
    resp = holder["resp"] = _open(prof["base"].rstrip("/") + "/chat/completions", headers, body)
    for ev in _sse(resp, cancelled):
        _err_in_event(ev)
        choice = (ev.get("choices") or [{}])[0]
        piece = (choice.get("delta") or {}).get("content")
        if piece:
            on_delta(piece)


def stream_anthropic(prof, msgs, on_delta, cancelled, holder):
    base = prof["base"].rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    headers = {"x-api-key": prof["key"], "anthropic-version": "2023-06-01"}
    body = {"model": prof["model"], "max_tokens": 2048, "stream": True,
            "system": SYSTEM_PROMPT, "messages": [anthropic_msg(m) for m in msgs]}
    resp = holder["resp"] = _open(base + "/messages", headers, body)
    for ev in _sse(resp, cancelled):
        if ev.get("type") == "error":
            raise ChatError(str(ev.get("error", {}).get("message", ev)))
        if ev.get("type") == "content_block_delta":
            piece = (ev.get("delta") or {}).get("text")
            if piece:
                on_delta(piece)


def stream_gemini(prof, msgs, on_delta, cancelled, holder):
    model = prof["model"].replace("models/", "")
    url = f"{prof['base'].rstrip('/')}/models/{model}:streamGenerateContent?alt=sse"
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [gemini_msg(m) for m in msgs],
    }
    resp = holder["resp"] = _open(url, {"x-goog-api-key": prof["key"]}, body)
    for ev in _sse(resp, cancelled):
        _err_in_event(ev)
        for cand in ev.get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                if part.get("text"):
                    on_delta(part["text"])


STREAMERS = {"openai": stream_openai, "anthropic": stream_anthropic, "gemini": stream_gemini}


def list_models(pid, prof):
    pre = PRESET[pid]
    base = prof["base"].rstrip("/")
    if pre["fmt"] == "openai":
        headers = {"Authorization": "Bearer " + prof["key"]} if prof["key"] else {}
        data = json.load(_open(base + "/models", headers, timeout=15))
        return [m["id"] for m in data.get("data", []) if m.get("id")]
    if pre["fmt"] == "anthropic":
        b = base if base.endswith("/v1") else base + "/v1"
        data = json.load(_open(b + "/models?limit=50", {"x-api-key": prof["key"],
                                                         "anthropic-version": "2023-06-01"}, timeout=15))
        return [m["id"] for m in data.get("data", []) if m.get("id")]
    data = json.load(_open(base + "/models?pageSize=100", {"x-goog-api-key": prof["key"]}, timeout=15))
    skip = ("tts", "embedding", "imagen", "veo", "aqa", "native-audio")
    names = [m["name"].replace("models/", "") for m in data.get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    return [n for n in names if not any(k in n for k in skip)]


# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------

lock = threading.RLock()
page_open = False
page_w = 536
tile_size = "wide"
view_mode = "chat"        # "chat" | "settings"
draft = ""                # what's typed in the chat box
busy = False
last_error = ""
page_h = 268
prompt_n = 0          # bump to give Fenn a fresh, empty input box
expanded = False      # full-history view
cancel = threading.Event()
holder = {"resp": None}

# settings page draft
sd = {"pid": "", "model": "", "base": "", "key": ""}
sd_status = ("", "dim")
sd_models = []
sd_busy = False


def enter_settings():
    global view_mode, sd_status, sd_models
    pid = active_pid() or "ollama"
    choose_provider(pid)
    sd_status, sd_models = ("", "dim"), []
    view_mode = "settings"


def choose_provider(pid):
    global sd_models, sd_status
    prof = profile(pid)
    sd.update({"pid": pid, "model": prof["model"], "base": prof["base"], "key": ""})
    sd_models, sd_status = [], ("", "dim")


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def input_width(extra=0):
    return max(140, page_w - 90 - extra)


def model_line():
    pid = active_pid()
    if not pid:
        return "Not set up"
    return f"{PRESET[pid]['label']} \u00b7 {profile(pid)['model'] or 'no model'}"


SUGGESTIONS = [
    ("lightbulb", "Explain something simply", "Explain how Wi-Fi works, in simple terms."),
    ("envelope-simple", "Draft a short message", "Write a short, polite email declining a meeting invite."),
    ("code", "Help with code", "Show me how to reverse a string in Python."),
]


def prompt_id():
    return f"prompt{prompt_n}"


def clean_md(t):
    """Models love markdown; the notch shows plain text, so tidy the common bits."""
    t = re.sub(r"```[A-Za-z0-9_+-]*\n?", "", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t, flags=re.S)
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", t)
    t = re.sub(r"(?m)^(\s*)[*-]\s+", "\\1\u2022 ", t)
    return t.replace("`", "")


def tail_text(body, max_lines, cpl):
    """Last max_lines of body (estimated), so a streaming reply follows its end."""
    lines = []
    for para in body.split("\n"):
        lines.extend(textwrap.wrap(para, cpl) or [""])
    if len(lines) <= max_lines:
        return body, False
    return "\u2026 " + "\n".join(lines[-max_lines:]), True


def assistant_name():
    pid = active_pid()
    return (profile(pid)["model"] if pid else "") or "AI"


def user_bubble(m, short=False):
    body = m.get("content", "")
    if short and len(body) > 110:
        body = body[:107] + "\u2026"
    kids = [text("You", size="small", color="accent", bold=True)]
    files = m.get("files") or []
    if files:
        names = ", ".join(f["name"] for f in files)
        if short and len(names) > 60:
            names = names[:57] + "\u2026"
        kids.append(row([icon("paperclip", size=12, color="dim"),
                         text(names, size="small", color="dim", wrap=True)], gap=4))
    kids.append(text(body, wrap=True))
    return card(kids, padding=6, gap=2)


def assistant_block(m, streaming, limit=None):
    body = clean_md(m.get("content", ""))
    cut = False
    if streaming and not body:
        shown = text("Thinking\u2026", size="small", color="dim")
    else:
        if limit:
            body, cut = tail_text(body, limit[0], limit[1])
        shown = text(body + (" \u258d" if streaming else ""), wrap=True)
    head = [icon("sparkle", size=13, color="accent"), text(assistant_name(), size="small", color="dim", bold=True)]
    if cut:
        head += [node("spacer"), button("expand", label="Full reply", icon_name="arrows-out-simple",
                                         tooltip="Read the whole reply")]
    return column([row(head, gap=4), shown], gap=2)


def history_view():
    kids = [row([button("collapse", label="Back to chat", icon_name="arrow-left", primary=True)])]
    for m in messages[-MAX_SHOWN * 2:]:
        kids.append(user_bubble(m) if m["role"] == "user"
                    else assistant_block(m, busy and m is messages[-1]))
    return column(kids, gap=8, scroll=True)


def attachment_chips():
    """Small removable chips for staged files, or None."""
    if not pending:
        return None
    n = len(pending)
    per = (page_w - 24 - 6 * (n - 1)) // n
    limit = max(5, int(per / 7.2) - 8)     # leave room for the icon and the x button
    chips = []
    for i, f in enumerate(pending):
        chips.append(card([row([
            icon(KIND_ICON.get(f["kind"], "file"), size=14, color="accent"),
            text(short_name(f["name"], limit), size="small"),
            button(f"rm{i}", icon_name="x", tooltip="Remove " + f["name"]),
        ], gap=4, align="center")], padding=4))
    return row(chips, gap=6)


def chat_view():
    if not is_configured():
        return column([
            icon("plugs-connected", size=26, color="accent"),
            text("Connect an AI to start chatting", size="title", bold=True),
            text("Paste an API key, or use a local model like Ollama.", size="small", color="dim"),
            button("settings", label="Open settings", icon_name="gear", primary=True),
        ], align="center", justify="center", gap=6)

    if expanded and messages:
        return history_view()

    send = (button("stop", icon_name="stop", tooltip="Stop") if busy
            else button("send", icon_name="paper-plane-right", primary=True, tooltip="Send (Enter)"))
    input_row = row([
        button("attach", icon_name="plus", tooltip="Attach files (PDF, images, Word, text or code)"),
        text_input(prompt_id(), value=draft, hint="Message " + assistant_name() + "\u2026",
                   width=input_width(40)),
        send,
    ], gap=6)

    parts = []
    if last_error:
        parts.append(card([text(last_error, size="small", color="red", wrap=True)], padding=6))
    if not messages and not last_error:
        parts += [
            column([icon("sparkle", size=24, color="accent"),
                    text("How can I help?", size="title", bold=True)], align="center", gap=4),
            node("list", items=[{"id": f"sg{i}", "title": t, "icon": ic}
                                for i, (ic, t, _p) in enumerate(SUGGESTIONS)]),
        ]
    else:
        # Latest exchange only; older ones live in the history view.
        cpl = max(30, int((page_w - 24) / 7.2))
        lines = max(3, int((page_h - 140 - (34 if pending else 0)) / 17))
        last = messages[-2:]
        for m in last:
            if m["role"] == "user":
                parts.append(user_bubble(m, short=True))
            else:
                parts.append(assistant_block(m, busy and m is messages[-1], (lines, cpl)))
    chips = attachment_chips()
    if attach_error:
        parts.append(text(attach_error, size="small", color="red", wrap=True))
    if chips:
        parts.append(chips)
    parts += [node("spacer"), input_row]
    return column(parts, gap=8)


def settings_view():
    pid = sd["pid"]
    pre = PRESET[pid]
    saved = profile(pid)
    iw = input_width()
    kids = [
        node("chips", id="provider", options=[p["label"] for p in PRESETS],
             selected=[p["id"] for p in PRESETS].index(pid)),
        # Actions up top so they are always on screen.
        row([
            button("save", label="Save", icon_name="check", primary=True),
            button("test", label="Test", icon_name="lightning"),
            button("detect", label="Detect models", icon_name="magnifying-glass"),
        ], gap=6),
    ]
    msg, color = sd_status
    kids.append(text(msg or pre["help"], size="small", color=color if msg else "dim"))
    if pre["key"]:
        if saved["key"]:
            k = saved["key"]
            hint = ("saved \u2713 \u2026" + k[-4:] if len(k) > 8 else "saved \u2713") + " (type to replace)"
        else:
            hint = "paste your API key" + ("" if pre["key_required"] else " (optional)")
        key_row = [icon("key", size=16, color="accent"), text_input("key", value=sd["key"], hint=hint, width=iw)]
        if saved["key"]:
            key_row.append(button("clear_key", icon_name="trash", tooltip="Forget saved key"))
        kids.append(row(key_row, gap=6))
    kids.append(row([icon("cpu", size=16, color="accent"),
                     text_input("model", value=sd["model"], hint="model name", width=iw)],
                    gap=6))
    if pre["edit_base"]:
        kids.append(row([icon("link", size=16, color="accent"),
                         text_input("base", value=sd["base"], hint="server URL", width=iw)],
                        gap=6))
    if sd_models:
        shown = sd_models[:MAX_MODELS_SHOWN]
        kids.append(text(f"{len(sd_models)} models found. Tap one to use it, then Save.",
                         size="small", color="dim"))
        kids.append(node("list", items=[
            {"id": f"mp{i}", "title": ("\u2713 " if n == sd["model"] else "") + n, "icon": "cpu"}
            for i, n in enumerate(shown)]))
    return column(kids, gap=6, scroll=True)


def page_actions():
    if view_mode == "settings":
        return [{"id": "back", "icon": "chat-circle", "tooltip": "Back to chat"}]
    return [
        {"id": "new_chat", "icon": "note-pencil", "tooltip": "New chat"},
        {"id": "regen", "icon": "arrow-clockwise", "tooltip": "Regenerate last reply"},
        {"id": "copy_last", "icon": "copy", "tooltip": "Copy last reply"},
        {"id": "history", "icon": "clock-counter-clockwise", "tooltip": "All messages"},
        {"id": "settings", "icon": "gear", "tooltip": "Settings"},
    ]


def tile_view():
    sub = "Thinking\u2026" if busy else (profile(active_pid())["model"] if is_configured() else "Tap to set up")
    if tile_size == "small":
        return column([icon("chat-circle-dots", size=22, color="accent")],
                      align="center", justify="center")
    if tile_size == "large":
        return column([
            icon("chat-circle-dots", size=24, color="accent"),
            text("AI Chat", size="title", bold=True),
            text(sub, size="small", color="dim"),
            button("open", label="Ask", icon_name="chat-circle-dots", primary=True, width=0),
        ], align="center", justify="center", gap=4)
    return row([
        icon("chat-circle-dots", size=26, color="accent"),
        column([text("AI Chat", size="title", bold=True), text(sub, size="small", color="dim")], gap=0),
    ], align="center", gap=8)


def render_page():
    view = settings_view() if view_mode == "settings" else chat_view()
    emit({"type": "page", "view": view, "actions": page_actions()})


def render():
    emit({"type": "tile", "view": tile_view()})
    render_page()
    emit({"type": "active", "active": busy})


def save_history():
    try:
        save_json("history", messages[-MAX_HISTORY_SAVED:])
    except OSError as e:
        log(f"could not save history: {e}", "warn")
    gc_attachments()


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

def send_prompt(prompt, files=None):
    """files=None sends (and clears) the staged chips; a list resends those instead."""
    global busy, draft, last_error, view_mode, prompt_n, expanded, attach_error
    prompt = (prompt or "").strip()
    if busy or not (prompt or files or (files is None and pending)):
        return
    if not is_configured():
        enter_settings()
        render_page()
        return
    with lock:
        if files is None:
            files = list(pending)
            pending.clear()
        msg = {"role": "user", "content": prompt or DEFAULT_FILE_PROMPT}
        if files:
            msg["files"] = files
        messages.append(msg)
        prompt = msg["content"]
        messages.append({"role": "assistant", "content": ""})
        busy, draft, last_error, attach_error = True, "", "", ""
        prompt_n += 1
        expanded = False
    cancel.clear()
    render()
    threading.Thread(target=chat_worker, args=(prompt,), daemon=True).start()


def chat_worker(prompt):
    global busy, draft, last_error, prompt_n
    pid = active_pid()
    prof, pre = profile(pid), PRESET[pid]
    error = ""
    last_draw = [0.0]

    def on_delta(piece):
        with lock:
            if len(messages[-1]["content"]) > MAX_REPLY_CHARS:
                cancel.set()
                return
            messages[-1]["content"] += piece
        now = time.monotonic()
        if now - last_draw[0] > 0.12:
            last_draw[0] = now
            render_page()

    with lock:
        hist = [dict(m) for m in messages[:-1]][-MAX_HISTORY_SENT:]
    while hist and hist[0]["role"] != "user":
        hist.pop(0)

    try:
        wire = [wire_message(m, prof) for m in hist]
        STREAMERS[pre["fmt"]](prof, wire, on_delta, cancel.is_set, holder)
    except ChatError as e:
        error = str(e)
    except Exception as e:
        if not cancel.is_set():
            error = f"{type(e).__name__}: {e}"
    finally:
        try:
            if holder["resp"]:
                holder["resp"].close()
        except Exception:
            pass
        holder["resp"] = None

    with lock:
        if not messages[-1]["content"].strip():
            messages.pop()          # empty assistant reply
            asked = messages.pop()  # the question that got no answer
            if not cancel.is_set():
                # put it (and its files) back so the user can retry
                draft = "" if prompt == DEFAULT_FILE_PROMPT else prompt
                pending[:0] = asked.get("files") or []
                del pending[MAX_ATTACH:]
                prompt_n += 1
        busy = False
        last_error = error
    save_history()
    render()
    if not page_open and not error:
        emit({"type": "badge", "badge": {"kind": "pulse"}})


def stop_chat():
    cancel.set()
    try:
        if holder["resp"]:
            holder["resp"].close()
    except Exception:
        pass


def new_chat():
    global last_error, draft, prompt_n, expanded, attach_error
    if busy:
        stop_chat()
    with lock:
        messages.clear()
        pending.clear()
        last_error, draft, attach_error = "", "", ""
        prompt_n += 1
        expanded = False
    save_history()
    render()


def regenerate():
    if busy:
        return
    with lock:
        i = len(messages) - 1
        while i >= 0 and messages[i]["role"] != "user":
            i -= 1
        if i < 0:
            return
        prompt = messages[i]["content"]
        files = list(messages[i].get("files") or [])
        del messages[i:]
    send_prompt(prompt, files=files)


def copy_last():
    for m in reversed(messages):
        if m["role"] == "assistant" and m["content"].strip():
            emit({"type": "copy", "text": m["content"]})
            return


# ---------------------------------------------------------------------------
# Settings actions
# ---------------------------------------------------------------------------

def draft_profile():
    pid = sd["pid"]
    saved = profile(pid)
    return {"pid": pid, "model": sd["model"].strip(),
            "base": sd["base"].strip() or PRESET[pid]["base"],
            "key": sd["key"].strip() or saved["key"]}


def validate(prof, pre):
    if pre["key_required"] and not prof["key"]:
        return "This provider needs an API key."
    if not prof["model"]:
        return "Enter a model name (or press Detect models)."
    return ""


def save_settings():
    global sd_status
    pid, pre = sd["pid"], PRESET[sd["pid"]]
    prof = draft_profile()
    problem = validate(prof, pre)
    if problem:
        sd_status = (problem, "red")
        return render_page()
    sealed = None
    if sd["key"].strip():
        try:
            sealed = seal(sd["key"].strip())
        except ChatError as e:
            sd_status = (str(e), "red")
            return render_page()
    entry = settings["profiles"].setdefault(pid, {})
    entry["model"], entry["base_url"] = prof["model"], prof["base"]
    if sealed:
        entry["key_scheme"], entry["key_enc"] = sealed
    settings["active"] = pid
    try:
        save_json("settings", settings)
        sd_status = ("Saved. You're ready to chat.", "green")
    except OSError as e:
        sd_status = (f"Could not save: {e}", "red")
    sd["key"] = ""
    render()


def clear_key():
    global sd_status
    entry = settings["profiles"].get(sd["pid"])
    if entry:
        entry.pop("key_enc", None)
        entry.pop("key_scheme", None)
        save_json("settings", settings)
    sd["key"] = ""
    sd_status = ("Saved key removed.", "dim")
    render()


def run_in_thread(fn):
    global sd_busy
    if sd_busy:
        return
    sd_busy = True

    def wrapper():
        global sd_busy
        try:
            fn()
        finally:
            sd_busy = False
            render_page()

    threading.Thread(target=wrapper, daemon=True).start()


def test_connection():
    global sd_status
    pid, pre = sd["pid"], PRESET[sd["pid"]]
    prof = draft_profile()
    problem = validate(prof, pre)
    if problem:
        sd_status = (problem, "red")
        return render_page()
    sd_status = ("Testing\u2026", "dim")
    render_page()

    def work():
        global sd_status
        done = threading.Event()

        def tick():
            global sd_status
            t0 = time.monotonic()
            while not done.wait(1.5):
                if done.is_set():
                    break
                n = int(time.monotonic() - t0)
                sd_status = (f"Testing\u2026 {n}s (a local model can take a while to load)", "dim")
                render_page()

        ticker = threading.Thread(target=tick, daemon=True)
        ticker.start()
        got = []
        try:
            STREAMERS[pre["fmt"]](prof, [{"role": "user", "content": "Reply with just the word OK."}],
                                  got.append, lambda: False, {"resp": None})
            reply = "".join(got).strip()[:40]
            result = (f"Works. The model said: {reply or '(empty reply)'}", "green")
        except ChatError as e:
            result = (str(e), "red")
        except Exception as e:
            result = (f"{type(e).__name__}: {e}", "red")
        done.set()
        ticker.join(timeout=3)
        sd_status = result

    run_in_thread(work)


def detect_models():
    global sd_status
    pid = sd["pid"]
    prof = draft_profile()
    if PRESET[pid]["key_required"] and not prof["key"]:
        sd_status = ("Add your API key first, then Detect.", "red")
        return render_page()
    sd_status = ("Looking for models\u2026", "dim")
    render_page()

    def work():
        global sd_status, sd_models
        try:
            found = list_models(pid, prof)
            sd_models = found
            if not found:
                sd_status = ("No models found. For Ollama, run: ollama pull llama3.2", "orange")
            else:
                if sd["model"] not in found:
                    sd["model"] = found[0]
                sd_status = (f"Found {len(found)} model(s). Pick one and Save.", "green")
        except ChatError as e:
            sd_status = (str(e), "red")
        except Exception as e:
            sd_status = (f"{type(e).__name__}: {e}", "red")

    run_in_thread(work)


# ---------------------------------------------------------------------------
# Events from Fenn
# ---------------------------------------------------------------------------

def value_of(msg):
    v = msg.get("value")
    if isinstance(v, str) and v[:1] in "{[":
        try:
            return json.loads(v)
        except ValueError:
            pass
    return v


def on_event(msg):
    global draft, view_mode, sd_status, expanded
    i, kind, v = msg.get("id"), msg.get("kind"), value_of(msg)

    if isinstance(i, str) and i.startswith("prompt"):
        if i != prompt_id():
            return              # late event from an input box we already replaced
        if isinstance(v, str):
            draft = v
        if kind == "submit":
            send_prompt(draft)
    elif i == "send":
        send_prompt(draft)
    elif i == "attach":
        open_picker()
    elif isinstance(i, str) and i.startswith("rm") and i[2:].isdigit():
        remove_pending(int(i[2:]))
        render_page()
    elif i == "stop":
        stop_chat()
    elif i == "new_chat":
        new_chat()
    elif i == "copy_last":
        copy_last()
    elif i == "regen":
        regenerate()
    elif i == "history":
        expanded = not expanded
        render_page()
    elif i == "expand":
        expanded = True
        render_page()
    elif i == "collapse":
        expanded = False
        render_page()
    elif isinstance(i, str) and i.startswith("sg") and i[2:].isdigit():
        n = int(i[2:])
        if n < len(SUGGESTIONS):
            send_prompt(SUGGESTIONS[n][2])
    elif i == "settings":
        enter_settings()
        render_page()
    elif i == "back":
        view_mode = "chat"
        expanded = False
        render()
    elif i == "open":
        emit({"type": "open_page"})
    elif i in ("key", "model", "base"):
        if isinstance(v, str):
            sd[i] = v
    elif i == "provider" and isinstance(v, dict):
        idx = v.get("index", 0)
        if 0 <= idx < len(PRESETS):
            choose_provider(PRESETS[idx]["id"])
            render_page()
    elif isinstance(i, str) and i.startswith("mp") and i[2:].isdigit():
        n = int(i[2:])
        if n < min(len(sd_models), MAX_MODELS_SHOWN):
            sd["model"] = sd_models[n]
            render_page()
    elif i == "save":
        save_settings()
    elif i == "test":
        test_connection()
    elif i == "detect":
        detect_models()
    elif i == "clear_key":
        clear_key()


def handle(msg):
    global page_open, page_w, page_h, tile_size
    t = msg.get("type")
    if t == "hello":
        render()
    elif t == "layout":
        if msg.get("target") == "page":
            page_w = int(msg.get("width") or page_w)
            page_h = int(msg.get("height") or page_h)
            render_page()
        else:
            tile_size = msg.get("size") or tile_size
            emit({"type": "tile", "view": tile_view()})
    elif t == "theme":
        render()
    elif t in ("drop", "files_dropped", "file_drop", "drag_drop"):
        # UNVERIFIED: the real drop message shape comes from Fenn's protocol docs.
        paths = paths_from_drop(msg)
        if paths and view_mode == "chat":
            add_files(paths)
            render_page()
    elif t == "page_open":
        page_open = True
        emit({"type": "badge", "badge": None})
        render_page()
    elif t == "page_close":
        page_open = False
    elif t in ("hotkey", "tile_click"):
        if t == "hotkey":
            emit({"type": "open_page"})
    elif t == "event":
        on_event(msg)
    elif t == "shutdown":
        stop_chat()
        save_history()


def main():
    migrate_plain_keys()
    gc_attachments()
    render()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            handle(msg)
        except Exception as e:  # keep the plugin alive, show the error in Fenn's log
            import traceback
            traceback.print_exc(file=sys.stderr)
            log(f"{type(e).__name__}: {e}", "error")
        if msg.get("type") == "shutdown":
            break


if __name__ == "__main__":
    main()
