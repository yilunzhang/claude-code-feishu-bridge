"""回复语言提醒的判定:群里最近一条明确的消息不是英文,本轮回复却几乎全是英文。

只查「非英文提问 → 英文回复」一个方向(模型默认语言是英文,反方向基本不出错)。
判定只看字符:ASCII 以外的字母(中/日/韩/俄/阿拉伯/泰…)算非英文;用拉丁字母书写的非英文语言
(法/德/西/越…)按字符分不出来,不在覆盖范围内。代码块、行内代码、链接/路径、带 `_` 或 `.` 的标识符
(如 reconcile_daemon_code_identity、hooklib.py)不计。
已知边界:回复里不加代码块直接贴大段英文日志可能被误判为英文;以英文为主但带一句中文的回复不会提醒。
阈值依据:在历史转发记录上回放,命中的英文回复非英文字母占比都 ≤1.4%,正常回复最低 32.5%。"""
import re
import unicodedata

_FENCE = re.compile(r"```.*?```", re.S)
_INLINE = re.compile(r"`[^`\n]*`")
# 只匹配 ASCII 可见字符,免得把紧挨着的中文一起吞掉
_URL_PATH = re.compile(r"https?://[!-~]+|[!-~]*/[!-~]*")
_IDENT = re.compile(r"[A-Za-z0-9]+(?:[._][A-Za-z0-9]+)+")

# 非英文字母至少占该段字母的 1/3(n >= a/2;一个汉字的信息量顶几个英文字母)才算这段是非英文
QUESTION_MIN_NON_ASCII = 4      # 任一行:至少这么多非英文字母
QUESTION_HEAD_MIN_NON_ASCII = 2 # 首行冒号前那段(通常是指令,如「看下 log」后面接日志):至少这么多
QUESTION_MIN_ASCII = 8          # 没有非英文行时,至少这么多英文字母才算明确是英文
REPLY_MIN_LETTERS = 40          # 回复太短不判(如「1/14。」这类进度回复)
REPLY_MAX_NON_ASCII_SHARE = 0.10


def _prose(s):
    s = _URL_PATH.sub(" ", _INLINE.sub(" ", _FENCE.sub(" ", s or "")))
    return _IDENT.sub(" ", s)


def _letters(s):
    """→ (非英文字母数, 英文字母数)。符号、数字、emoji 都不算字母。"""
    non_ascii = ascii_ = 0
    for ch in s:
        if ch.isascii():
            if ch.isalpha():
                ascii_ += 1
        elif unicodedata.category(ch).startswith("L"):
            non_ascii += 1
    return non_ascii, ascii_


def question_language(text):
    """→ "non_en" | "en" | None(太短看不出,如「A」「ok」)。
    首行冒号前的指令、或任一行是非英文即算非英文 —— 中文指令 + 粘贴的大段英文日志仍判非英文。"""
    p = _prose(text)
    lines = [ln for ln in p.splitlines() if ln.strip()]
    if lines and _non_en(re.split(r"[:：]", lines[0], maxsplit=1)[0], QUESTION_HEAD_MIN_NON_ASCII):
        return "non_en"
    if any(_non_en(ln, QUESTION_MIN_NON_ASCII) for ln in lines):
        return "non_en"
    return "en" if _letters(p)[1] >= QUESTION_MIN_ASCII else None


def _non_en(segment, min_non_ascii):
    n, a = _letters(segment)
    return n >= min_non_ascii and n >= a * 0.5


def reply_is_english(text):
    n, a = _letters(_prose(text))
    if n + a < REPLY_MIN_LETTERS:
        return False
    return n / (n + a) < REPLY_MAX_NON_ASCII_SHARE
