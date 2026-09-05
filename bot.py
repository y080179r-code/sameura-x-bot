#!/usr/bin/env python3
"""Sameura Dam storage-rate bot for X.

- Polls the official MLIT river.go.jp page.
- Falls back to Water Resources Agency daily snapshot.
- Uses adaptive posting frequency based on the current storage rate.
- Posts threshold crossings and unusually large moves immediately.
- Keeps source URLs OUT of automated posts to avoid URL-priced posts.
- Persists a small rolling history in state.json for 24h deltas.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

JST = ZoneInfo("Asia/Tokyo")

RIVER_URL = "https://www1.river.go.jp/cgi-bin/DspDamData.exe?ID=1368080700010&KIND=3"
WATER_URL = "https://www.water.go.jp/yoshino/yoshino/"
X_POST_URL = "https://api.x.com/2/tweets"
STATE_PATH = Path(os.getenv("STATE_PATH", "state.json"))

THRESHOLDS = sorted({float(x) for x in os.getenv("THRESHOLDS", "5,10,15,20,25,30,40,50,60,70,80,90").split(",") if x.strip()})
MAX_POSTS_PER_DAY = int(os.getenv("MAX_POSTS_PER_DAY", "30"))
MIN_POST_INTERVAL_MINUTES = int(os.getenv("MIN_POST_INTERVAL_MINUTES", "20"))
RAPID_CHANGE_PT = float(os.getenv("RAPID_CHANGE_PT", "1.0"))

# Adaptive cadence. Format: upper_bound:hours. The first matching upper bound wins.
# Default behavior:
#   <10%  -> every 1h
#   <20%  -> every 2h
#   <40%  -> every 3h
#   <60%  -> every 6h
#   <80%  -> every 12h
#   >=80% -> every 24h
CADENCE_BANDS = [
    (10.0, float(os.getenv("CADENCE_UNDER_10_HOURS", "1"))),
    (20.0, float(os.getenv("CADENCE_UNDER_20_HOURS", "2"))),
    (40.0, float(os.getenv("CADENCE_UNDER_40_HOURS", "3"))),
    (60.0, float(os.getenv("CADENCE_UNDER_60_HOURS", "6"))),
    (80.0, float(os.getenv("CADENCE_UNDER_80_HOURS", "12"))),
    (float("inf"), float(os.getenv("CADENCE_80_PLUS_HOURS", "24"))),
]
DRY_RUN = os.getenv("DRY_RUN", "").lower() in {"1", "true", "yes", "on"}
FORCE_POST = os.getenv("FORCE_POST", "").lower() in {"1", "true", "yes", "on"}

HEADERS = {
    "User-Agent": "SameuraReservoirBot/4.2 (public-interest dam status bot)",
    "Accept-Language": "ja,en;q=0.5",
}


@dataclass
class Decision:
    post: bool
    reason: str
    kind: str = "regular"  # regular / rapid / threshold / first / forced
    threshold: float | None = None
    threshold_direction: str | None = None
    report_hour: int | None = None


def clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def number(s: str | None) -> float | None:
    if s is None:
        return None
    s = clean(s).replace(",", "").replace("％", "").replace("%", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def parse_date(text: str, now: datetime) -> tuple[int, int, int] | None:
    text = clean(text)
    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", text)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))

    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})", text)
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    year = now.year
    if now.month == 1 and month == 12:
        year -= 1
    elif now.month == 12 and month == 1:
        year += 1
    return year, month, day


def _extract_river_rows(html: bytes, now: datetime) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[dict[str, Any]] = []
    current_date: tuple[int, int, int] | None = None

    for tr in soup.find_all("tr"):
        cells = [clean(x.get_text(" ", strip=True)) for x in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if not cells:
            continue

        for c in cells[:2]:
            parsed = parse_date(c, now)
            if parsed:
                current_date = parsed
                break

        time_idx = None
        hh = mm = None
        for i, c in enumerate(cells):
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", c)
            if m:
                time_idx = i
                hh, mm = int(m.group(1)), int(m.group(2))
                break

        if time_idx is None or current_date is None or hh is None or mm is None:
            continue

        # Standard table order after time:
        # 流域平均雨量, 貯水量, 流入量, 放流量, 貯水率
        vals = cells[time_idx + 1 :]
        if len(vals) < 5:
            continue

        rainfall = number(vals[0])
        storage = number(vals[1])
        inflow = number(vals[2])
        outflow = number(vals[3])
        rate = number(vals[4])
        if rate is None or not 0 <= rate <= 100:
            continue

        y, mo, da = current_date
        try:
            observed = datetime(y, mo, da, hh, mm, tzinfo=JST)
        except ValueError:
            continue

        candidates.append(
            {
                "observed_at": observed.isoformat(),
                "rate": rate,
                "rainfall_mm_h": rainfall,
                "storage_thousand_m3": storage,
                "inflow_m3_s": inflow,
                "outflow_m3_s": outflow,
                "source": "国土交通省 川の防災情報",
                "source_url": RIVER_URL,
                "source_kind": "realtime",
            }
        )

    return candidates


def fetch_river() -> dict[str, Any]:
    """Fetch real-time table. Re-fetches the parent page if its temporary iframe expires."""
    now = datetime.now(JST)
    s = requests.Session()
    s.headers.update(HEADERS)
    last_error: Exception | None = None

    for attempt in range(3):
        try:
            parent_r = s.get(RIVER_URL, timeout=20)
            parent_r.raise_for_status()
            parent = BeautifulSoup(parent_r.content, "html.parser")

            iframe = parent.find("iframe")
            if not iframe or not iframe.get("src"):
                # Some renderings may inline the table. Try the parent itself first.
                rows = _extract_river_rows(parent_r.content, now)
                if rows:
                    return max(rows, key=lambda x: x["observed_at"])
                raise RuntimeError("river.go.jp: data iframe not found")

            data_url = urljoin(RIVER_URL, iframe["src"])
            data_r = s.get(data_url, timeout=20)
            data_r.raise_for_status()
            rows = _extract_river_rows(data_r.content, now)
            if not rows:
                raise RuntimeError("river.go.jp: no usable observation rows found")
            return max(rows, key=lambda x: x["observed_at"])
        except Exception as e:  # noqa: BLE001 - deliberately retry all network/parse errors
            last_error = e
            if attempt < 2:
                time.sleep(1.2 * (attempt + 1))

    raise RuntimeError(f"river.go.jp failed after retries: {last_error}")


def fetch_water_fallback() -> dict[str, Any]:
    """Fallback: Water Resources Agency page. Usually a daily 00:00 snapshot."""
    s = requests.Session()
    s.headers.update(HEADERS)
    r = s.get(WATER_URL, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "html.parser")
    text = clean(soup.get_text(" ", strip=True))

    mt = re.search(
        r"貯水率情報[（(]\s*(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2})時現在\s*[）)]",
        text,
    )
    if mt:
        y, mo, da, hh = map(int, mt.groups())
        observed = datetime(y, mo, da, hh, 0, tzinfo=JST)
    else:
        observed = datetime.now(JST).replace(minute=0, second=0, microsecond=0)

    mr = re.search(r"早明浦ダム\s+(\d+(?:\.\d+)?)\s*%", text)
    if not mr:
        raise RuntimeError("water.go.jp fallback: storage rate not found")

    return {
        "observed_at": observed.isoformat(),
        "rate": float(mr.group(1)),
        "rainfall_mm_h": None,
        "storage_thousand_m3": None,
        "inflow_m3_s": None,
        "outflow_m3_s": None,
        "source": "水資源機構 吉野川本部",
        "source_url": WATER_URL,
        "source_kind": "daily_fallback",
    }


def fetch_observation() -> dict[str, Any]:
    try:
        return fetch_river()
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] real-time source failed: {e}", file=sys.stderr)
        return fetch_water_fallback()


def default_state() -> dict[str, Any]:
    return {
        "last_observed_at": None,
        "last_seen_rate": None,
        "last_posted_at": None,
        "last_posted_observed_at": None,
        "last_posted_rate": None,
        "last_post_id": None,
        "history": [],
        "report_slots": {},
        "daily_post_counts": {},
    }


def load_state() -> dict[str, Any]:
    state = default_state()
    if not STATE_PATH.exists():
        return state
    try:
        loaded = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            state.update(loaded)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] failed to read state.json: {e}", file=sys.stderr)
    return state


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def as_dt(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).astimezone(JST)
    except ValueError:
        return None


def update_history(state: dict[str, Any], obs: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
    history = state.setdefault("history", [])
    prev = history[-1] if history else None
    is_new = not history or history[-1].get("observed_at") != obs["observed_at"]

    if is_new:
        history.append(
            {
                "observed_at": obs["observed_at"],
                "rate": obs["rate"],
                "storage_thousand_m3": obs.get("storage_thousand_m3"),
                "inflow_m3_s": obs.get("inflow_m3_s"),
                "outflow_m3_s": obs.get("outflow_m3_s"),
                "rainfall_mm_h": obs.get("rainfall_mm_h"),
            }
        )

    cutoff = datetime.now(JST) - timedelta(hours=72)
    history[:] = [x for x in history if (as_dt(x.get("observed_at")) or cutoff) >= cutoff]
    return prev, is_new


def find_24h_reference(state: dict[str, Any], observed: datetime) -> dict[str, Any] | None:
    target = observed - timedelta(hours=24)
    history = state.get("history", [])
    candidates: list[tuple[float, dict[str, Any]]] = []
    for item in history:
        dt = as_dt(item.get("observed_at"))
        if not dt or dt >= observed:
            continue
        distance = abs((dt - target).total_seconds())
        # Accept observations roughly 18–30h behind.
        age = (observed - dt).total_seconds() / 3600
        if 18 <= age <= 30:
            candidates.append((distance, item))
    return min(candidates, key=lambda x: x[0])[1] if candidates else None


def crossed_threshold(old: float | None, new: float) -> tuple[str, float] | None:
    if old is None:
        return None
    # On a large jump, prefer the threshold nearest the new value.
    crossed: list[tuple[str, float]] = []
    for t in THRESHOLDS:
        if old >= t > new:
            crossed.append(("down", t))
        elif old < t <= new:
            crossed.append(("up", t))
    if not crossed:
        return None
    return crossed[-1] if new > old else crossed[0]


def cadence_hours(rate: float) -> float:
    """Return normal posting interval for the current storage rate."""
    for upper_bound, hours in CADENCE_BANDS:
        if rate < upper_bound:
            return hours
    return 24.0


def cadence_label(rate: float) -> str:
    hours = cadence_hours(rate)
    if hours == 1:
        return "1時間ごと"
    if hours < 1:
        return f"{int(hours * 60)}分ごと"
    return f"{hours:g}時間ごと"


def posts_today(state: dict[str, Any], now: datetime) -> int:
    return int(state.get("daily_post_counts", {}).get(now.strftime("%Y-%m-%d"), 0))


def increment_posts_today(state: dict[str, Any], now: datetime) -> None:
    key = now.strftime("%Y-%m-%d")
    counts = state.setdefault("daily_post_counts", {})
    counts[key] = int(counts.get(key, 0)) + 1
    for old_key in list(counts):
        try:
            d = datetime.strptime(old_key, "%Y-%m-%d").date()
            if (now.date() - d).days > 7:
                del counts[old_key]
        except ValueError:
            pass


def choose_decision(obs: dict[str, Any], state: dict[str, Any], prev: dict[str, Any] | None, is_new: bool, now: datetime) -> Decision:
    if FORCE_POST:
        return Decision(True, "FORCE_POST", "forced")

    if posts_today(state, now) >= MAX_POSTS_PER_DAY:
        return Decision(False, "daily safety cap reached")

    last_posted = as_dt(state.get("last_posted_at"))
    if last_posted and (now - last_posted) < timedelta(minutes=MIN_POST_INTERVAL_MINUTES):
        return Decision(False, "minimum post interval")

    rate = float(obs["rate"])
    old_rate = float(prev["rate"]) if prev and prev.get("rate") is not None else None

    # Threshold crossings always get priority, independent of the normal cadence.
    crossing = crossed_threshold(old_rate, rate)
    if is_new and crossing:
        direction, threshold = crossing
        return Decision(True, f"crossed {threshold}% {direction}", "threshold", threshold, direction)

    # If the reservoir moves sharply since the previous post, don't wait for the normal cadence.
    last_posted_rate = state.get("last_posted_rate")
    if is_new and last_posted_rate is not None:
        move = rate - float(last_posted_rate)
        if abs(move) >= RAPID_CHANGE_PT:
            return Decision(True, f"rapid change {move:+.1f}pt", "rapid")

    if state.get("last_posted_observed_at") is None:
        return Decision(True, "first observation", "first")

    # Normal posting is adaptive: the lower the storage rate, the more frequently we post.
    interval = timedelta(hours=cadence_hours(rate))
    if is_new and (last_posted is None or now - last_posted >= interval):
        return Decision(True, f"adaptive cadence {cadence_label(rate)}", "regular")

    return Decision(False, f"waiting for adaptive cadence ({cadence_label(rate)})")


def fmt(v: float | None, digits: int = 1) -> str:
    if v is None:
        return "—"
    if float(v).is_integer():
        return f"{int(v):,}"
    return f"{float(v):,.{digits}f}"


def mood(delta: float | None, rate: float) -> str:
    """A little personality without making drought alerts feel flippant."""
    if delta is None:
        return "💧"

    # Below 10%, keep the tone more alert even if the latest observation improved.
    if rate < 10:
        if delta >= 1.0:
            return "😊💧"
        if delta > 0:
            return "🙂💧"
        if delta <= -1.0:
            return "😰🚨"
        if delta < 0:
            return "😥⚠️"
        return "😐⚠️"

    if delta >= 2.0:
        return "🤩🎉"
    if delta >= 1.0:
        return "😄🌊"
    if delta >= 0.5:
        return "😊💧"
    if delta > 0:
        return "🙂↗️"
    if delta <= -2.0:
        return "😱🚨"
    if delta <= -1.0:
        return "😰⚠️"
    if delta <= -0.5:
        return "😥↘️"
    if delta < 0:
        return "🥲↘️"
    return "😐➡️"


def change_emoji(delta: float | None) -> str:
    """Emoji for the change line. Reservoir status itself is shown separately."""
    if delta is None:
        return ""
    if delta >= 2.0:
        return "🤩"
    if delta >= 1.0:
        return "😄"
    if delta >= 0.5:
        return "😊"
    if delta > 0:
        return "🙂"
    if delta <= -2.0:
        return "😱"
    if delta <= -1.0:
        return "😰"
    if delta <= -0.5:
        return "😥"
    if delta < 0:
        return "🥲"
    return "😐"


def movement_comment(delta: float | None, rate: float) -> str | None:
    """Short optional comment used only for clearly noticeable moves."""
    if delta is None:
        return None
    if rate < 10:
        if delta >= 1.0:
            return "少し持ち直しました💧"
        if delta <= -1.0:
            return "厳しい状況が続いています⚠️"
        return None
    if delta >= 2.0:
        return "ぐっと回復しました！🎉"
    if delta >= 1.0:
        return "いい感じに増えてます😊"
    if delta <= -2.0:
        return "大きく減少しています🚨"
    if delta <= -1.0:
        return "やや大きめの減少です⚠️"
    return None


def build_post(obs: dict[str, Any], state: dict[str, Any], prev: dict[str, Any] | None, decision: Decision) -> str:
    observed = as_dt(obs["observed_at"]) or datetime.now(JST)
    rate = float(obs["rate"])
    prev_rate = float(prev["rate"]) if prev and prev.get("rate") is not None else None
    delta_prev = rate - prev_rate if prev_rate is not None else None

    ref24 = find_24h_reference(state, observed)
    delta24 = rate - float(ref24["rate"]) if ref24 and ref24.get("rate") is not None else None

    if decision.kind == "threshold" and decision.threshold is not None:
        t = fmt(decision.threshold)
        if decision.threshold_direction == "down":
            title = f"🚨 早明浦ダム {t}%を下回りました"
        else:
            title = f"🙌 早明浦ダム {t}%を回復しました"
    elif decision.kind == "rapid":
        title = "⚡ 早明浦ダム 貯水率が大きく変化"
    else:
        variants = ["🏞️ 早明浦ダム 貯水率", "💧 早明浦ダム 定点観測", "📊 早明浦ダム 最新値"]
        title = variants[observed.hour % len(variants)]

    # Keep the reservoir status line simple and consistent:
    # - always show a water drop
    # - below 10%, put a siren BEFORE the percentage
    if rate < 10:
        rate_text = f"🚨 {rate:.1f}% 💧"
    else:
        rate_text = f"{rate:.1f}% 💧"

    lines = [
        title,
        f"{observed:%m/%d %H:%M}　{rate_text}",
    ]

    # Put the playful/emotional emoji on the change line instead of the level line.
    if delta_prev is not None:
        change = change_emoji(delta_prev)
        line = f"前回比 {delta_prev:+.1f}pt {change}"
        if delta24 is not None:
            line += f"｜24時間 {delta24:+.1f}pt"
        lines.append(line)
    elif delta24 is not None:
        lines.append(f"24時間 {delta24:+.1f}pt")

    comment = movement_comment(delta_prev, rate)
    if comment and decision.kind not in {"threshold"}:
        lines.append(comment)

    if obs.get("storage_thousand_m3") is not None:
        lines.append(f"貯水量 {fmt(obs['storage_thousand_m3'])}×10³m³")

    if obs.get("inflow_m3_s") is not None or obs.get("outflow_m3_s") is not None:
        lines.append(f"流入 {fmt(obs.get('inflow_m3_s'))} / 放流 {fmt(obs.get('outflow_m3_s'))} m³/s")

    if obs.get("rainfall_mm_h") is not None and float(obs["rainfall_mm_h"]) > 0:
        lines.append(f"流域平均雨量 {fmt(obs['rainfall_mm_h'])} mm/h")

    lines += ["出典：" + obs["source"], "#早明浦ダム #吉野川"]

    # Intentionally no source URL in auto-posts.
    text = "\n".join(lines)
    if len(text) > 270:
        # Defensive shortening if a future source label gets longer.
        text = text.replace("出典：国土交通省 川の防災情報", "出典：国交省 川の防災情報")
    return text


def post_to_x(text: str) -> dict[str, Any]:
    from requests_oauthlib import OAuth1

    required = ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]
    missing = [x for x in required if not os.getenv(x)]
    if missing:
        raise RuntimeError("missing X credentials: " + ", ".join(missing))

    auth = OAuth1(
        os.environ["X_API_KEY"],
        os.environ["X_API_SECRET"],
        os.environ["X_ACCESS_TOKEN"],
        os.environ["X_ACCESS_TOKEN_SECRET"],
    )
    r = requests.post(X_POST_URL, auth=auth, json={"text": text}, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"X API error {r.status_code}: {r.text}")
    return r.json()


def main() -> int:
    now = datetime.now(JST)
    obs = fetch_observation()
    state = load_state()
    prev, is_new = update_history(state, obs)

    state["last_observed_at"] = obs["observed_at"]
    state["last_seen_rate"] = obs["rate"]

    decision = choose_decision(obs, state, prev, is_new, now)
    print(json.dumps(obs, ensure_ascii=False, indent=2))
    print(f"[INFO] decision: {decision.post} / {decision.kind} / {decision.reason}")

    if not decision.post:
        save_state(state)
        return 0

    text = build_post(obs, state, prev, decision)
    print("\n--- POST PREVIEW ---\n" + text + "\n--------------------")

    if DRY_RUN:
        print("[INFO] DRY_RUN=1: Xへは投稿していません")
        save_state(state)
        return 0

    result = post_to_x(text)
    print("[INFO] posted:", json.dumps(result, ensure_ascii=False))

    state["last_posted_at"] = now.isoformat()
    state["last_posted_observed_at"] = obs["observed_at"]
    state["last_posted_rate"] = obs["rate"]
    state["last_post_id"] = result.get("data", {}).get("id")
    increment_posts_today(state, now)

    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
