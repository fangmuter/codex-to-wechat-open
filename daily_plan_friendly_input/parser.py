"""Small deterministic, non-executing friendly-input translator.

The parser extracts only typed planner facts.  It does not interpret free
text as roadmap progress, commands, paths, or confirmation.
"""
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .models import FriendlyParseResult
from daily_plan_recommendation.models import Energy


_SEPARATORS = r"，,；;。\n"
_CN_HOURS = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}


def _hour(value: str) -> int:
    return int(value) if value.isdigit() else _CN_HOURS.get(value, 0)


def _clock_hour(period: str, value: str) -> int:
    hour = _hour(value)
    if period in {'下午', '晚上'} and hour < 12:
        hour += 12
    return hour


def _period_label(period: str | None) -> str | None:
    return '上午' if period == '早上' else period


def _clean(value: str) -> str:
    value = re.sub(r"^(?:明天|明日|今天|昨天|我|想|也|再)\s*", "", value.strip())
    value = re.sub(r"\s+", " ", value)
    return value.strip(" ，,；;。")


def _fixed_events(text: str) -> list[dict]:
    fixed: list[dict] = []
    # "下午的课到四点" is an unavailable constraint; no start time is
    # invented.
    for match in re.finditer(
        r"(?P<period>上午|早上|下午|晚上)[^，,；;。\n]{0,18}?(?:课|课程)[^，,；;。\n]{0,8}?(?:到|至)\s*(?P<hour>\d{1,2}|[一二三四五六七八九十]+)\s*点",
        text,
    ):
        fixed.append({
            "title": f"{_period_label(match.group('period'))}课程",
            "preferred_period": _period_label(match.group("period")),
            "unavailable_until": f"{_clock_hour(match.group('period'), match.group('hour')):02d}:00",
            "estimated_minutes": 0,
            "consumes_available_capacity": False,
        })
    # "上午满课" carries a constraint but no fabricated clock time.
    for match in re.finditer(r"(?P<period>上午|早上|下午|晚上)\s*(?:满课|有课)", text):
        period = _period_label(match.group("period"))
        if any(x.get("preferred_period") == period and x.get("unavailable_until") for x in fixed):
            continue
        fixed.append({
            "title": f"{period}课程",
            "preferred_period": period,
            "estimated_minutes": 0,
            "consumes_available_capacity": False,
        })
    # Named events such as "下午有示例课" / "明天有会议".
    pattern = r"(?:明天|明日)?\s*(上午|早上|下午|晚上)?\s*(?:有|安排)?\s*([^，,；;。\n]*?(?:课|会议|考试|实验(?!报告)))"
    for match in re.finditer(pattern, text):
        title = _clean(match.group(2))
        if (not title or title in {x["title"] for x in fixed} or title in {"课", "课程", "有课", "满课", "的课", "的课程"}
                or title.startswith("课挺") or title.startswith("课不少")
                or title.startswith("课很多")):
            continue
        item = {"title": title, "estimated_minutes": 0,
                "consumes_available_capacity": False}
        if match.group(1):
            item["preferred_period"] = _period_label(match.group(1))
        fixed.append(item)
    return fixed


def _required_tasks(text: str) -> list[dict]:
    required: list[dict] = []
    pattern = r"(?:明天|明日)?\s*(?:必须|一定要|要交|要完成|需要完成|需要)\s*(?:把|将)?\s*([^，,；;。\n]+)"
    for match in re.finditer(pattern, text):
        title = _clean(match.group(1))
        title = re.sub(r"^(?:完成|做完|把)\s*", "", title)
        title = re.sub(r"(?:了|吧)$", "", title).strip()
        if title and title not in {x["title"] for x in required}:
            required.append({"title": title, "estimated_minutes": 60})
    # Natural shorthand used by the one-message path.
    for match in re.finditer(r"还有\s*一项\s*([^，,；;。\n]+)", text):
        title = _clean(match.group(1))
        if title and not any(x["title"] == title for x in required):
            required.append({"title": title, "estimated_minutes": 60})
    return required


def _carryovers(text: str) -> list[dict]:
    carry: list[dict] = []
    pattern = r"(?:今天|昨天)?\s*([^，,；;。\n]+?)\s*(?:还有|仍有)\s*([^，,；;。\n]+?)\s*(?:没做(?:完)?|未完成|没完成)"
    for match in re.finditer(pattern, text):
        subject = _clean(match.group(1))
        remainder = _clean(match.group(2))
        subject = re.sub(r"^(?:学习|复习)\s*", "", subject)
        if not subject:
            subject = remainder
        if subject and subject not in {x["title"] for x in carry}:
            carry.append({"title": subject, "estimated_minutes": 60})
    for match in re.finditer(r"(?:顺延|延期)\s*[:：]?\s*([^，,；;。\n]+)", text):
        title = _clean(match.group(1))
        if title and title not in {x["title"] for x in carry}:
            carry.append({"title": title, "estimated_minutes": 60})
    return carry


def _normalise_subject(value: str) -> str:
    """Apply stable presentation aliases without inferring a new task."""
    value = value.strip(" ，,；;。")
    aliases = {"cpp": "C++", "c++": "C++", "matlab": "MATLAB", "stm32": "STM32"}
    return aliases.get(value.lower(), value)


def _arranged_titles(fragment: str) -> list[str]:
    """Extract explicit next-day actions from one sentence fragment.

    Review/past-tense fragments are filtered before this helper is called.
    The small vocabulary keeps parsing deterministic and avoids promoting
    filler such as ``的话我觉得得`` to a task title.
    """
    value = _clean(fragment)
    value = re.sub(r"^(?:明天|明日)\s*", "", value)
    for _ in range(4):
        value = re.sub(r"^(?:的话|我觉得|觉得|得|要|应该|可能|然后|再|也|就|希望|打算)\s*", "", value)
    value = value.strip(" ，,；;。")
    if not value:
        return []

    # Verb-first phrases, including a deterministic split for "学一下课程A和
    # 课程B". A subject is never invented when the phrase has no action.
    front = re.match(
        r"^(?P<verb>继续(?:快速)?推进|继续(?:往后)?学|推进|复习|学习|学(?:一下)?)"
        r"\s*(?:一点|一下|一些)?\s*(?P<subjects>.+)$",
        value,
    )
    if front:
        verb = front.group("verb")
        if verb.startswith("继续") and "推进" in verb:
            label = "继续推进"
        elif verb.startswith("继续"):
            label = "继续学习"
        elif verb.startswith("学") or verb == "学习":
            label = "学习"
        else:
            label = verb
        subjects = [
            _normalise_subject(part)
            for part in re.split(r"\s*(?:、|和|以及|并且)\s*", front.group("subjects"))
            if part.strip()
        ]
        return [f"{label}{subject}" for subject in subjects]

    # Subject-first continuation, e.g. "课程A继续往后看". Keep the subject
    # at the front so the resulting action remains recognizable and stable.
    continuation = re.match(
        r"^(?P<subject>.+?)继续(?:往后)?(?P<verb>推进|学|看)(?P<rest>.*)$",
        value,
    )
    if continuation:
        subject = _normalise_subject(continuation.group("subject"))
        return [f"{subject}继续学习{continuation.group('rest')}"]

    # Subject-first shorthand, e.g. "课程A推进一点" or "课程B复习一下".
    suffix = re.match(
        r"^(?P<subject>.+?)(?P<verb>推进|复习|学习|学(?:一下)?)"
        r"\s*(?:一点|一下|一些)?$",
        value,
    )
    if suffix:
        verb = suffix.group("verb")
        label = "学习" if verb.startswith("学") or verb == "学习" else verb
        return [f"{label}{_normalise_subject(suffix.group('subject'))}"]
    return []


def _arranged_tasks(text: str, required: list[dict], carry: list[dict]) -> list[dict]:
    arranged: list[dict] = []
    required_titles = {x["title"] for x in required}
    carry_titles = {x["title"] for x in carry}
    for fragment in re.split(f"[{_SEPARATORS}]", text):
        fragment = fragment.strip()
        # Do not turn today's review into tomorrow's commitment.  This also
        # covers common past-tense wording such as "推进了/完成了".
        if (not fragment or fragment.startswith(("今天", "昨天"))
                or re.search(r"(?:已经|完成了|做完了|推进了|学会了|学到了)", fragment)
                or any(marker in fragment for marker in ("必须", "一定要", "要交", "截止", "没推进", "未推进", "没完成", "未完成", "没做完"))):
            continue
        if not re.search(r"(?:继续|推进|复习|学习|学一下|想做|想学|安排|看一看|看看|往后)", fragment):
            continue
        for title in _arranged_titles(fragment):
            if title in required_titles or title in carry_titles:
                continue
            if title and title not in {x["title"] for x in arranged}:
                arranged.append({"title": title, "estimated_minutes": 45,
                                 "kind": "self_arranged"})
    return arranged


def _review_fields(text: str) -> tuple[list[str], list[str]]:
    completed: list[str] = []
    unfinished: list[str] = []
    for match in re.finditer(r"(?:今天|昨天)?\s*(?:学习了|完成了|做完了|收尾了)\s*([^，,；;。\n]+)", text):
        value = _clean(match.group(1))
        if value and value not in completed:
            completed.append(value)
    for match in re.finditer(r"([^，,；;。\n]+?)\s*(?:没推进|未推进|没完成|未完成|没做完)", text):
        value = _clean(match.group(1))
        if value and value not in unfinished:
            unfinished.append(value)
    return completed, unfinished


def _capacity(text: str) -> tuple[int | None, str | None]:
    match = re.search(r"(?:大概|约|大约)?\s*(\d+(?:[.．]\d+)?)\s*(?:个)?小时", text)
    if match:
        try:
            hours = Decimal(match.group(1).replace("．", "."))
            minutes = int((hours * Decimal(60)).to_integral_value(rounding=ROUND_HALF_UP))
        except (InvalidOperation, ValueError):
            return None, None
        return minutes, "晚上" if "晚上" in text else "user-stated"
    match = re.search(r"(?:大概|约|大约)?\s*(\d+)\s*分钟", text)
    if match:
        return int(match.group(1)), "user-stated"
    match = re.search(r"(\d{1,2})\s*(?:点|时)(?:\s*(\d{1,2})\s*分)?\s*(?:到|至|-|–)\s*(\d{1,2})\s*(?:点|时)", text)
    if match:
        start = int(match.group(1)) * 60 + int(match.group(2) or 0)
        end = int(match.group(3)) * 60
        if end > start:
            return end - start, match.group(0)
    return None, None


def _energy(text: str) -> Energy | None:
    if re.search(r"很累|比较累|没精神|没那么有精力|状态差|疲惫|精力低", text):
        return Energy.LOW
    if re.search(r"精神不错|状态很好|很有精力|精力高|状态好", text):
        return Energy.HIGH
    if re.search(r"状态一般|一般|还行|正常|中等|精力中", text):
        return Energy.NORMAL
    return None


def parse_friendly(text):
    if not isinstance(text, str) or not text.strip():
        return FriendlyParseResult(ambiguity_flags=("empty",))
    text = text.strip()
    fixed = _fixed_events(text)
    required = _required_tasks(text)
    carry = _carryovers(text)
    arranged = _arranged_tasks(text, required, carry)
    completed, unfinished = _review_fields(text)
    minutes, window = _capacity(text)
    energy = _energy(text)
    unresolved: list[str] = []
    flags: list[str] = []
    if minutes is None and re.search(r"(?:可用|空闲|有时间|看情况|时间够不够)", text):
        flags.append("available_time_ambiguous")
    if energy is None and "状态" in text:
        flags.append("energy_ambiguous")
    if re.search(r"运行|执行|powershell|shell|打开\s*[A-Za-z]:\\|删除文件|rm\s+-rf", text, re.I):
        flags.append("unsupported_command_or_path")
        unresolved.append(text)
    elif not (fixed or required or carry or arranged or completed or unfinished or minutes is not None or energy is not None):
        unresolved.append(text)
    return FriendlyParseResult(tuple(fixed), tuple(required), tuple(carry), minutes,
                                window, energy, tuple(unresolved), tuple(flags),
                                tuple(arranged), tuple(completed), tuple(unfinished))
