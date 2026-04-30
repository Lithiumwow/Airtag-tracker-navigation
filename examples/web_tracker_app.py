import argparse
import asyncio
import hashlib
import json
import logging
import os
import math
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from _login import get_account_async
from aiohttp import web

from findmy import FindMyAccessory, KeyPair

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Path where login session will be stored.
STORE_PATH = "account.json"

# URL to LOCAL anisette server. Set to None to use built-in Anisette generator.
ANISETTE_SERVER = None

# Path where Anisette libraries will be stored.
ANISETTE_LIBS_PATH = "ani_libs.bin"

# Path for location history database
LOCATION_HISTORY_PATH = "location_history.json"

# User-editable tag label (persists across restarts)
TRACKER_CONFIG_PATH = "tracker_config.json"

# Cached road-snapped polylines per calendar day (survives restarts; avoids flaky browser→OSRM)
SNAPPED_ROUTES_CACHE_PATH = "snapped_routes_cache.json"

OSRM_BASE = "https://router.project-osrm.org"

# Calendar day boundaries use the same zone as stored timestamps in LocationHistoryDB.add
APP_TIMEZONE = ZoneInfo("America/New_York")
TIMELINE_RENDER_MAX_POINTS = 2000
TIMELINE_SNAP_MAX_POINTS = 700


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 points in kilometers."""
    r = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def distance_along_entries(entries: list) -> float:
    """Sum segment lengths along chronologically ordered points."""
    if len(entries) < 2:
        return 0.0
    ordered = sorted(entries, key=lambda e: e.get("timestamp") or "")
    total = 0.0
    for i in range(1, len(ordered)):
        a, b = ordered[i - 1], ordered[i]
        total += haversine_km(
            float(a["latitude"]),
            float(a["longitude"]),
            float(b["latitude"]),
            float(b["longitude"]),
        )
    return total


def downsample_entries_uniform(entries: list, max_points: int) -> list:
    """Uniformly reduce list size while preserving first/last points."""
    if max_points <= 0 or len(entries) <= max_points:
        return entries
    if max_points == 1:
        return [entries[0]]
    last_idx = len(entries) - 1
    step = last_idx / (max_points - 1)
    out = []
    seen = set()
    for i in range(max_points):
        idx = int(round(i * step))
        idx = min(last_idx, max(0, idx))
        if idx in seen:
            continue
        seen.add(idx)
        out.append(entries[idx])
    if out[-1] is not entries[-1]:
        out[-1] = entries[-1]
    return out


def load_tracker_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Could not load tracker config %s: %s", path, exc)
        return {}


def save_tracker_config(path: str, data: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        logger.error("Could not save tracker config %s: %s", path, exc)


def load_snap_cache() -> dict:
    p = Path(SNAPPED_ROUTES_CACHE_PATH)
    if not p.exists():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Could not load snap cache: %s", exc)
        return {}


def save_snap_cache(data: dict) -> None:
    try:
        with open(SNAPPED_ROUTES_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        logger.error("Could not save snap cache: %s", exc)


def fingerprint_entries(ordered: list) -> str:
    payload = json.dumps(
        [(float(e["latitude"]), float(e["longitude"]), str(e.get("timestamp", ""))) for e in ordered],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def coords_close_ll(a: list[float], b: list[float], eps: float = 1e-7) -> bool:
    return abs(a[0] - b[0]) < eps and abs(a[1] - b[1]) < eps


def merge_matching_geometries_osrm(data: dict) -> list[list[float]]:
    out: list[list[float]] = []
    for m in data.get("matchings") or []:
        geom = (m.get("geometry") or {}).get("coordinates") or []
        if not geom:
            continue
        seg = [[c[1], c[0]] for c in geom]
        if not out:
            out.extend(seg)
        elif seg:
            if coords_close_ll(out[-1], seg[0]):
                out.extend(seg[1:])
            else:
                out.extend(seg)
    return out


async def osrm_get_json(session: aiohttp.ClientSession, url: str) -> dict | None:
    for attempt in range(3):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=90)) as resp:
                if resp.status != 200:
                    txt = await resp.text()
                    logger.warning("OSRM HTTP %s: %s", resp.status, txt[:160])
                    if attempt < 2:
                        await asyncio.sleep(0.55 * (attempt + 1))
                        continue
                    return None
                return await resp.json()
        except Exception as exc:
            logger.warning("OSRM request failed (attempt %s): %s", attempt + 1, exc)
            if attempt < 2:
                await asyncio.sleep(0.55 * (attempt + 1))
                continue
            return None
    return None


async def route_pairwise_osrm(session: aiohttp.ClientSession, chunk: list[dict]) -> list[list[float]]:
    merged: list[list[float]] = []
    for i in range(len(chunk) - 1):
        a, b = chunk[i], chunk[i + 1]
        pair_str = f'{float(a["longitude"])},{float(a["latitude"])};{float(b["longitude"])},{float(b["latitude"])}'
        url = f"{OSRM_BASE}/route/v1/driving/{pair_str}?geometries=geojson&overview=full"
        rd = await osrm_get_json(session, url)
        if rd and rd.get("routes") and rd["routes"][0].get("geometry"):
            coords = rd["routes"][0]["geometry"]["coordinates"]
            seg = [[c[1], c[0]] for c in coords]
        else:
            seg = [
                [float(a["latitude"]), float(a["longitude"])],
                [float(b["latitude"]), float(b["longitude"])],
            ]
        if not merged:
            merged.extend(seg)
        elif seg:
            if coords_close_ll(merged[-1], seg[0]):
                merged.extend(seg[1:])
            else:
                merged.extend(seg)
    return merged


async def match_chunk_osrm(session: aiohttp.ClientSession, chunk: list[dict]) -> list[list[float]]:
    if len(chunk) == 1:
        c = chunk[0]
        return [[float(c["latitude"]), float(c["longitude"])]]
    coord_str = ";".join(f'{float(l["longitude"])},{float(l["latitude"])}' for l in chunk)
    radiuses = ";".join(["75"] * len(chunk))
    url = f"{OSRM_BASE}/match/v1/driving/{coord_str}?geometries=geojson&overview=full&radiuses={radiuses}"
    part: list[list[float]] = []
    data = await osrm_get_json(session, url)
    if data:
        part = merge_matching_geometries_osrm(data)
        if not part and data.get("routes") and data["routes"][0].get("geometry"):
            coords = data["routes"][0]["geometry"]["coordinates"]
            part = [[c[1], c[0]] for c in coords]
    if not part:
        route_url = f"{OSRM_BASE}/route/v1/driving/{coord_str}?geometries=geojson&overview=full"
        rd = await osrm_get_json(session, route_url)
        if rd and rd.get("routes") and rd["routes"][0].get("geometry"):
            coords = rd["routes"][0]["geometry"]["coordinates"]
            part = [[c[1], c[0]] for c in coords]
    if not part and len(chunk) <= 30:
        part = await route_pairwise_osrm(session, chunk)
    if not part:
        part = [[float(l["latitude"]), float(l["longitude"])] for l in chunk]
    return part


async def snap_locations_to_roads(session: aiohttp.ClientSession, ordered: list[dict]) -> list[list[float]]:
    """Road-snapped [[lat, lon], ...] for Leaflet (same semantics as previous client OSRM logic)."""
    if len(ordered) < 2:
        return []
    pts = [{"latitude": float(e["latitude"]), "longitude": float(e["longitude"])} for e in ordered]
    max_chunk = 85
    step = max_chunk - 1
    merged: list[list[float]] = []
    start = 0
    n = len(pts)
    while start < n:
        end = min(start + max_chunk, n)
        chunk = pts[start:end]
        if len(chunk) == 1:
            only = chunk[0]
            pt = [only["latitude"], only["longitude"]]
            if not merged or not coords_close_ll(merged[-1], pt):
                merged.append(pt)
            start += step
            continue
        part = await match_chunk_osrm(session, chunk)
        if not merged:
            merged.extend(part)
        elif part:
            if coords_close_ll(merged[-1], part[0]):
                merged.extend(part[1:])
            else:
                merged.extend(part)
        start += step
    return merged if merged else [[float(e["latitude"]), float(e["longitude"])] for e in pts]


class LocationHistoryDB:
    """Simple JSON-based location history database"""
    
    def __init__(self, path: str):
        self.path = Path(path)
        self.load()
    
    def load(self):
        """Load history from file"""
        if self.path.exists():
            try:
                with open(self.path, 'r') as f:
                    self.history = json.load(f)
            except Exception as e:
                logger.error(f"Error loading history: {e}")
                self.history = []
        else:
            self.history = []
    
    def save(self):
        """Save history to file"""
        try:
            with open(self.path, 'w') as f:
                json.dump(self.history, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving history: {e}")
    
    def add(self, latitude: float, longitude: float, accuracy: int, timestamp: str):
        """Add a location entry"""
        # Check if this location is different enough from the last one
        if self.history and len(self.history) > 0:
            last = self.history[-1]
            if last['latitude'] == latitude and last['longitude'] == longitude:
                return  # Skip duplicate
        
        entry = {
            'latitude': latitude,
            'longitude': longitude,
            'accuracy': accuracy,
            'timestamp': timestamp,
            'date': timestamp.split()[0]  # YYYY-MM-DD
        }
        self.history.append(entry)
        self.save()
    
    def get_all(self) -> list:
        """Get all history"""
        return self.history
    
    def get_by_date(self, date: str) -> list:
        """Get history for a specific date (YYYY-MM-DD)"""
        return [h for h in self.history if h['date'] == date]
    
    def get_date_range(self) -> dict:
        """Get min and max calendar dates present in history (YYYY-MM-DD)."""
        if not self.history:
            return {'first': None, 'last': None}
        dates = [h['date'] for h in self.history if h.get('date')]
        return {'first': min(dates), 'last': max(dates)}


HTML_PAGE = """<!doctype html>
<html lang="en" data-theme="light">
<head>
  <meta charset="utf-8" />
  <script>
    (function () {
      try {
        var t = localStorage.getItem("ui-theme");
        document.documentElement.setAttribute("data-theme", t === "light" || t === "dark" ? t : "light");
      } catch (e) {
        document.documentElement.setAttribute("data-theme", "light");
      }
    })();
  </script>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>FindMy Web Tracker - Live Location</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@300;400;500;600;700&display=swap" rel="stylesheet" />
  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
    crossorigin=""
  />
  <style>
    :root {
      --font: "Chakra Petch", system-ui, -apple-system, "Segoe UI", sans-serif;
      --bg-map: #0b0f14;
      --bg-bar: rgba(15, 18, 26, 0.82);
      --bg-panel: rgba(18, 22, 32, 0.96);
      --bg-elevated: rgba(28, 34, 48, 0.55);
      --border: rgba(255, 255, 255, 0.07);
      --border-focus: rgba(56, 189, 248, 0.45);
      --text: #f1f5f9;
      --text-secondary: #94a3b8;
      --text-muted: #64748b;
      --accent: #38bdf8;
      --accent-muted: rgba(56, 189, 248, 0.14);
      --success: #34d399;
      --success-muted: rgba(52, 211, 153, 0.12);
      --warning: #fbbf24;
      --danger: #f87171;
      --radius-sm: 8px;
      --radius-md: 12px;
      --radius-lg: 14px;
      --shadow-bar: 0 4px 24px rgba(0, 0, 0, 0.35);
      --shadow-panel: 0 18px 48px rgba(0, 0, 0, 0.5);
      /* Leaflet zoom control height + gap; locate button stacks above zoom (bottom-right) */
      --map-zoom-stack-height: 76px;
      --map-float-gap: 12px;
    }

    @media (pointer: coarse) {
      :root {
        --map-zoom-stack-height: 92px;
      }
    }

    html[data-theme="light"] {
      --bg-map: #cbd5e1;
      --bg-bar: rgba(255, 255, 255, 0.94);
      --bg-panel: rgba(255, 255, 255, 0.98);
      --bg-elevated: rgba(241, 245, 249, 0.92);
      --border: rgba(15, 23, 42, 0.14);
      --border-focus: rgba(14, 165, 233, 0.55);
      --text: #0f172a;
      --text-secondary: #334155;
      --text-muted: #64748b;
      --accent: #0284c7;
      --accent-muted: rgba(14, 165, 233, 0.14);
      --success: #059669;
      --success-muted: rgba(5, 150, 105, 0.14);
      --warning: #b45309;
      --danger: #dc2626;
      --shadow-bar: 0 4px 20px rgba(15, 23, 42, 0.1);
      --shadow-panel: 0 16px 40px rgba(15, 23, 42, 0.12);
    }

    html {
      color-scheme: dark;
    }

    html[data-theme="light"] {
      color-scheme: light;
    }

    * { margin: 0; padding: 0; box-sizing: border-box; }
    html, body, #map {
      height: 100%;
      width: 100%;
      font-family: var(--font);
      -webkit-font-smoothing: antialiased;
      text-rendering: optimizeLegibility;
    }

    body {
      background: var(--bg-map);
      color: var(--text);
      overflow: hidden;
    }

    /* Animated Marker */
    @keyframes pulse-glow {
      0% { 
        box-shadow: 0 0 0 0 rgba(76, 175, 80, 0.8),
                    inset 0 0 10px rgba(255, 255, 255, 0.5);
      }
      50% {
        box-shadow: 0 0 0 15px rgba(76, 175, 80, 0.3),
                    inset 0 0 20px rgba(255, 255, 255, 0.3);
      }
      100% { 
        box-shadow: 0 0 0 25px rgba(76, 175, 80, 0),
                    inset 0 0 10px rgba(255, 255, 255, 0.1);
      }
    }

    @keyframes spin-arrow {
      0% { transform: rotate(0deg) scale(1); }
      50% { transform: rotate(180deg) scale(1.1); }
      100% { transform: rotate(360deg) scale(1); }
    }

    @keyframes bounce {
      0%, 100% { transform: translateY(0); }
      50% { transform: translateY(-8px); }
    }

    .location-marker {
      width: 50px;
      height: 50px;
      display: flex;
      align-items: center;
      justify-content: center;
      background: linear-gradient(135deg, #34d399 0%, #059669 100%);
      border: 4px solid white;
      border-radius: 50%;
      font-size: 28px;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
      animation: pulse-glow 2s ease-in-out infinite, bounce 3s ease-in-out infinite;
      position: relative;
      overflow: hidden;
    }

    .location-marker::before {
      content: '';
      position: absolute;
      top: 50%;
      left: 50%;
      transform: translate(-50%, -50%);
      width: 100%;
      height: 100%;
      background: radial-gradient(circle, rgba(255,255,255,0.3) 0%, transparent 70%);
      animation: spin-arrow 4s linear infinite;
    }

    .location-marker::after {
      content: '';
      position: absolute;
      top: 2px;
      left: 50%;
      width: 3px;
      height: 15px;
      background: white;
      border-radius: 2px;
      transform: translateX(-50%);
      animation: spin-arrow 6s linear infinite reverse;
    }

    /* Top chrome: one row of tabs + refresh; panels open below (no overlap) */
    .ui-overlay {
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      z-index: 1100;
      pointer-events: none;
      display: flex;
      flex-direction: column;
      align-items: stretch;
    }

    .ui-overlay > * {
      pointer-events: auto;
    }

    .top-bar {
      display: flex;
      flex-wrap: nowrap;
      align-items: center;
      gap: 12px;
      padding: 8px 14px;
      padding-top: max(10px, env(safe-area-inset-top));
      padding-left: max(14px, env(safe-area-inset-left));
      padding-right: max(14px, env(safe-area-inset-right));
      background: var(--bg-bar);
      border-bottom: 1px solid var(--border);
      backdrop-filter: blur(20px) saturate(1.35);
      -webkit-backdrop-filter: blur(20px) saturate(1.35);
      box-shadow: var(--shadow-bar);
    }

    .nav-status-region {
      flex: 1 1 0;
      min-width: 0;
      display: flex;
      align-items: center;
    }

    .nav-status-region .status {
      width: 100%;
    }

    .top-bar-trailing {
      display: flex;
      align-items: center;
      gap: 10px;
      flex: 0 0 auto;
    }

    .top-bar-btn {
      flex: 0 0 auto;
      min-width: 108px;
      padding: 10px 16px;
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      background: var(--bg-elevated);
      color: var(--text-secondary);
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.11em;
      text-transform: uppercase;
      cursor: pointer;
      -webkit-tap-highlight-color: transparent;
      transition: background 0.18s ease, border-color 0.18s ease, color 0.18s ease, box-shadow 0.18s ease;
    }

    .top-bar-btn:hover {
      background: rgba(36, 42, 58, 0.85);
      color: var(--text);
      border-color: rgba(255, 255, 255, 0.12);
    }

    .top-bar-btn.active {
      border-color: rgba(56, 189, 248, 0.55);
      background: var(--accent-muted);
      color: var(--text);
      box-shadow: inset 0 0 0 1px rgba(56, 189, 248, 0.12);
    }

    .refresh-pill {
      flex: 0 0 auto;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: rgba(15, 23, 42, 0.65);
      color: var(--text-secondary);
      padding: 8px 12px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      border: 1px solid var(--border);
      white-space: nowrap;
    }

    .refresh-pill .refresh-timer-num {
      font-variant-numeric: tabular-nums;
      color: var(--accent);
      font-size: 13px;
      font-weight: 700;
      letter-spacing: 0;
      text-transform: none;
    }

    .theme-toggle-btn {
      flex: 0 0 auto;
      padding: 8px 12px;
      border-radius: 999px;
      font-size: 10px;
      font-weight: 600;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      font-family: inherit;
      border: 1px solid var(--border);
      background: var(--bg-elevated);
      color: var(--text-secondary);
      cursor: pointer;
      -webkit-tap-highlight-color: transparent;
      transition: border-color 0.18s ease, color 0.18s ease, background 0.18s ease;
    }

    .theme-toggle-btn:hover {
      border-color: rgba(56, 189, 248, 0.35);
      color: var(--text);
    }

    html[data-theme="light"] .theme-toggle-btn:hover {
      border-color: rgba(14, 165, 233, 0.45);
    }

    html[data-theme="light"] .sheet {
      background: linear-gradient(180deg, rgba(248, 250, 252, 0.99) 0%, rgba(241, 245, 249, 0.99) 100%);
    }

    html[data-theme="light"] .status {
      background: rgba(255, 255, 255, 0.9);
      border-color: var(--border);
    }

    html[data-theme="light"] .status.error {
      background: rgba(254, 226, 226, 0.92);
      border-color: rgba(248, 113, 113, 0.4);
    }

    html[data-theme="light"] .refresh-pill {
      background: rgba(241, 245, 249, 0.95);
    }

    html[data-theme="light"] .top-bar-btn:hover {
      background: rgba(226, 232, 240, 0.96);
    }

    html[data-theme="light"] .timeline-btn:hover {
      background: rgba(226, 232, 240, 0.96);
    }

    .sheet {
      display: none;
      max-height: min(52vh, 420px);
      overflow-x: hidden;
      overflow-y: auto;
      -webkit-overflow-scrolling: touch;
      overscroll-behavior: contain;
      background: linear-gradient(180deg, rgba(12, 16, 24, 0.98) 0%, rgba(8, 10, 16, 0.99) 100%);
      border-bottom: 1px solid var(--border);
      box-shadow: var(--shadow-panel);
    }

    .sheet.open {
      display: block;
    }

    .sheet:focus {
      outline: none;
    }

    .sheet .timeline-panel {
      max-width: 560px;
      margin-left: auto;
      margin-right: auto;
    }

    /* Details: fixed bottom-left — open by default; compact; ▼/▲ toggles panel */
    .details-dock {
      position: fixed;
      left: max(10px, env(safe-area-inset-left));
      bottom: max(12px, env(safe-area-inset-bottom));
      z-index: 1150;
      display: flex;
      flex-direction: column-reverse;
      align-items: flex-start;
      gap: 6px;
      max-width: min(340px, calc(100vw - 24px));
      pointer-events: none;
    }

    .details-dock > * {
      pointer-events: auto;
    }

    .details-dock-toggle {
      width: 34px;
      height: 34px;
      padding: 0;
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      font-size: 13px;
      line-height: 1;
      font-family: inherit;
      color: var(--text-secondary);
      background: var(--bg-bar);
      backdrop-filter: blur(20px) saturate(1.35);
      -webkit-backdrop-filter: blur(20px) saturate(1.35);
      cursor: pointer;
      box-shadow: var(--shadow-bar);
      -webkit-tap-highlight-color: transparent;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      transition: border-color 0.18s ease, color 0.18s ease, background 0.18s ease;
    }

    .details-dock-toggle:hover {
      border-color: rgba(56, 189, 248, 0.4);
      color: var(--text);
    }

    .details-dock--open .details-dock-toggle {
      border-color: rgba(56, 189, 248, 0.45);
      color: var(--accent);
      background: var(--accent-muted);
    }

    .details-dock-panel {
      display: none;
      width: 100%;
      max-height: none;
      overflow-x: hidden;
      overflow-y: visible;
      border-radius: var(--radius-md);
      border: 1px solid var(--border);
      background: var(--bg-panel);
      box-shadow: var(--shadow-panel);
    }

    .details-dock--open .details-dock-panel {
      display: block;
    }

    .details-dock-panel:focus {
      outline: none;
    }

    .details-dock .info-panel {
      margin: 0;
      padding: 10px 12px 12px;
      border: none;
      border-radius: 0;
      box-shadow: none;
      background: transparent;
      font-size: 12px;
      line-height: 1.45;
    }

    .details-dock .info-panel .label {
      margin-top: 8px;
      font-size: 9px;
      letter-spacing: 0.08em;
    }

    .details-dock .info-panel .label:first-child {
      margin-top: 0;
    }

    .details-dock .info-value {
      font-size: 11px;
    }

    .details-dock .meta-subtle {
      font-size: 10px;
      margin-top: 4px;
    }

    .details-metrics {
      margin-top: 6px;
      padding-top: 6px;
      border-top: 1px solid var(--border);
      font-size: 10px;
      line-height: 1.45;
      color: var(--text-secondary);
    }

    .details-metric-line {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: 0 4px;
      margin-bottom: 3px;
    }

    .details-metric-line:last-child {
      margin-bottom: 0;
    }

    .details-k {
      color: var(--text-muted);
      font-weight: 600;
      font-size: 9px;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      margin-right: 3px;
    }

    .details-kv {
      display: inline-flex;
      align-items: baseline;
      flex-wrap: nowrap;
      gap: 3px;
      max-width: 100%;
    }

    .details-kv strong {
      font-weight: 600;
      color: var(--text);
      font-variant-numeric: tabular-nums;
      font-size: 11px;
    }

    .details-sep {
      color: var(--text-muted);
      opacity: 0.55;
      user-select: none;
      padding: 0 2px;
      font-weight: 400;
    }

    .details-dock .last-detected-box {
      padding: 8px 10px;
      margin-top: 4px;
    }

    .details-dock #details-placeholder.details-placeholder {
      margin: 0;
      padding: 10px 12px 12px;
      max-width: none;
      font-size: 11px;
      line-height: 1.4;
    }

    @media (max-width: 520px) {
      .top-bar {
        flex-wrap: wrap;
      }
      .nav-status-region {
        flex: 1 1 100%;
      }
      .top-bar-trailing {
        width: 100%;
        justify-content: flex-end;
      }
    }

    @media (min-width: 900px) {
      .top-bar {
        padding-left: max(20px, env(safe-area-inset-left));
        padding-right: max(20px, env(safe-area-inset-right));
      }
      .sheet {
        max-height: min(45vh, 380px);
      }
    }

    /* Inline status in top nav — compact, no scroll box */
    .status {
      color: var(--text-secondary);
      margin: 0;
      padding: 4px 10px;
      font-size: 10px;
      line-height: 1.25;
      font-weight: 500;
      background: rgba(10, 14, 22, 0.65);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 3px 8px;
      row-gap: 2px;
    }

    .status.error {
      color: rgba(254, 202, 202, 0.95);
      border-color: rgba(248, 113, 113, 0.4);
      background: rgba(127, 29, 29, 0.22);
    }

    .status.error .label {
      color: rgba(254, 202, 202, 0.98);
    }

    .status .label {
      font-weight: 600;
      color: var(--accent);
      margin: 0;
      font-size: 9px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      flex-shrink: 0;
    }

    .status > div:not(.label) {
      font-weight: 400;
      letter-spacing: 0.02em;
      font-size: 10px;
      margin: 0;
    }

    .status .status-hint {
      flex: 1 1 100%;
      font-size: 9px;
      line-height: 1.2;
      color: var(--text-muted);
      margin: 0;
      font-weight: 400;
    }

    .status .status-hint--warn {
      color: var(--warning);
    }

    .info-panel {
      color: var(--text-secondary);
      margin: 14px 16px 18px;
      padding: 16px 18px 20px;
      font-size: 13px;
      line-height: 1.65;
      background: var(--bg-panel);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      box-shadow: 0 1px 0 rgba(255, 255, 255, 0.04) inset;
    }

    .info-panel .label {
      color: var(--text-muted);
      font-weight: 600;
      margin-top: 14px;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }

    .info-panel .label:first-child {
      margin-top: 0;
    }

    .info-value {
      color: var(--text);
      font-family: ui-monospace, "Cascadia Code", "SF Mono", Menlo, monospace;
      font-size: 13px;
      font-weight: 600;
    }

    .stats-row {
      display: flex;
      justify-content: stretch;
      margin-top: 10px;
      gap: 10px;
      flex-wrap: wrap;
    }

    .stat {
      flex: 1;
      min-width: 108px;
      padding: 12px 14px;
      background: var(--bg-elevated);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
    }

    .stat-value {
      font-size: 15px;
      color: var(--text);
      font-weight: 700;
      font-variant-numeric: tabular-nums;
    }

    .stat-label {
      font-size: 10px;
      color: var(--text-muted);
      margin-top: 4px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }

    .meta-subtle {
      font-size: 11px;
      color: var(--text-muted);
      margin-top: 6px;
      line-height: 1.45;
    }

    .last-detected-box {
      background: rgba(251, 191, 36, 0.08);
      border: 1px solid rgba(251, 191, 36, 0.25);
      padding: 12px 14px;
      border-radius: var(--radius-md);
      margin-top: 6px;
    }

    .last-detected-box .info-value {
      color: var(--warning);
    }

    .last-detected-elapsed {
      font-size: 11px;
      color: rgba(251, 191, 36, 0.85);
      margin-top: 6px;
    }

    .details-placeholder {
      padding: 20px 24px;
      color: var(--text-muted);
      font-size: 13px;
      line-height: 1.55;
    }

    /* Path styling */
    .location-path {
      stroke: #34d399;
      stroke-width: 3;
      fill: none;
      opacity: 0.6;
      stroke-dasharray: 5, 5;
      stroke-linecap: round;
    }

    /* Animation for path */
    @keyframes dash-animation {
      to {
        stroke-dashoffset: 10;
      }
    }

    .location-path {
      animation: dash-animation 20s linear infinite;
    }

    /* Timeline (inside sheet) */
    .timeline-panel {
      color: var(--text);
      margin: 14px 16px 18px;
      padding: 16px 18px 20px;
      background: var(--bg-panel);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      box-shadow: 0 1px 0 rgba(255, 255, 255, 0.04) inset;
    }

    .timeline-header {
      font-weight: 600;
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 0.14em;
      margin-bottom: 14px;
      color: var(--text-muted);
      border-bottom: 1px solid var(--border);
      padding-bottom: 10px;
    }

    .timeline-controls {
      display: flex;
      gap: 10px;
      margin-bottom: 12px;
    }

    .live-overlay-toggle {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 12px;
      font-size: 12px;
      color: var(--text-secondary);
      user-select: none;
    }

    .live-overlay-toggle input[type="checkbox"] {
      width: 17px;
      height: 17px;
      border-radius: 4px;
      accent-color: var(--accent);
      cursor: pointer;
    }

    .timeline-btn {
      flex: 1;
      padding: 10px 14px;
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      background: var(--bg-elevated);
      color: var(--text-secondary);
      font-weight: 600;
      font-size: 11px;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      cursor: pointer;
      transition: background 0.18s ease, border-color 0.18s ease, color 0.18s ease;
    }

    .timeline-btn:hover {
      background: rgba(36, 42, 58, 0.88);
      color: var(--text);
      border-color: rgba(255, 255, 255, 0.12);
    }

    .timeline-btn.active {
      border-color: rgba(52, 211, 153, 0.45);
      background: var(--success-muted);
      color: var(--success);
    }

    .timeline-date-picker-row {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 12px;
      flex-wrap: wrap;
    }

    .timeline-date-picker-row label {
      font-size: 11px;
      color: var(--text-muted);
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }

    .timeline-date-picker-row input[type="date"] {
      flex: 1;
      min-width: 160px;
      padding: 10px 12px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--border);
      background: rgba(10, 14, 22, 0.72);
      color: var(--text);
      font-size: 14px;
      font-family: inherit;
    }

    .timeline-date-picker-row input[type="date"]:focus {
      outline: none;
      border-color: var(--border-focus);
      box-shadow: 0 0 0 3px var(--accent-muted);
    }

    .timeline-date-picker-row input[type="date"]::-webkit-calendar-picker-indicator {
      filter: invert(0.75);
      cursor: pointer;
      opacity: 0.9;
    }

    .timeline-slider-container {
      width: 100%;
      margin-bottom: 12px;
    }

    .timeline-slider {
      width: 100%;
      height: 7px;
      border-radius: 999px;
      background: linear-gradient(90deg, var(--success-muted), var(--accent-muted));
      outline: none;
      -webkit-appearance: none;
      appearance: none;
      cursor: pointer;
    }

    .timeline-slider::-webkit-slider-thumb {
      -webkit-appearance: none;
      appearance: none;
      width: 20px;
      height: 20px;
      border-radius: 50%;
      background: linear-gradient(145deg, #7dd3fc, var(--accent));
      cursor: pointer;
      box-shadow: 0 2px 12px rgba(56, 189, 248, 0.45);
      border: 2px solid rgba(255, 255, 255, 0.95);
      transition: transform 0.15s ease, box-shadow 0.15s ease;
    }

    .timeline-slider::-webkit-slider-thumb:hover {
      transform: scale(1.08);
      box-shadow: 0 4px 16px rgba(56, 189, 248, 0.55);
    }

    .timeline-slider::-moz-range-thumb {
      width: 20px;
      height: 20px;
      border-radius: 50%;
      background: linear-gradient(145deg, #7dd3fc, var(--accent));
      cursor: pointer;
      box-shadow: 0 2px 12px rgba(56, 189, 248, 0.45);
      border: 2px solid rgba(255, 255, 255, 0.95);
    }

    .timeline-slider::-moz-range-thumb:hover {
      transform: scale(1.08);
    }

    .timeline-display {
      text-align: center;
      padding: 12px 14px;
      background: var(--bg-elevated);
      border-radius: var(--radius-md);
      border: 1px solid var(--border);
      font-weight: 600;
      font-size: 12px;
      margin-top: 10px;
      color: var(--accent);
    }

    .timeline-display .date {
      font-size: 15px;
      color: var(--text);
      margin-bottom: 4px;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
    }

    .timeline-display .days {
      font-size: 11px;
      color: var(--text-muted);
      font-weight: 500;
    }

    .timeline-loading {
      display: none;
      margin-top: 12px;
      padding: 10px 12px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--border);
      background: var(--accent-muted);
      color: var(--accent);
      font-size: 12px;
      font-weight: 600;
    }

    .timeline-loading.active {
      display: block;
    }

    .current-tag-label {
      background: var(--bg-panel);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      padding: 6px 10px;
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.02em;
      box-shadow: var(--shadow-bar);
      backdrop-filter: blur(8px);
    }

    /* Center-on-tracker: above zoom controls, bottom-right stack */
    .locate-btn {
      position: fixed;
      right: max(14px, env(safe-area-inset-right));
      bottom: calc(
        max(16px, env(safe-area-inset-bottom)) + var(--map-zoom-stack-height) + var(--map-float-gap)
      );
      z-index: 1200;
      width: 54px;
      height: 54px;
      border: 1px solid var(--border);
      border-radius: 50%;
      background: var(--bg-bar);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      cursor: pointer;
      box-shadow: var(--shadow-panel);
      -webkit-tap-highlight-color: transparent;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: transform 0.15s ease, border-color 0.15s ease, box-shadow 0.15s ease;
    }

    .locate-btn .blue-dot {
      display: inline-block;
      width: 14px;
      height: 14px;
      border-radius: 50%;
      background: radial-gradient(circle at 30% 30%, #bae6fd, var(--accent));
      box-shadow: 0 0 0 3px var(--accent-muted), 0 0 14px rgba(56, 189, 248, 0.45);
      animation: blue-dot-pulse 1.6s ease-in-out infinite;
    }

    @keyframes blue-dot-pulse {
      0% { transform: scale(0.96); box-shadow: 0 0 0 2px var(--accent-muted), 0 0 8px rgba(56, 189, 248, 0.35); }
      50% { transform: scale(1.05); box-shadow: 0 0 0 5px rgba(56, 189, 248, 0.12), 0 0 16px rgba(56, 189, 248, 0.5); }
      100% { transform: scale(0.96); box-shadow: 0 0 0 2px var(--accent-muted), 0 0 8px rgba(56, 189, 248, 0.35); }
    }

    .tag-walker {
      position: relative;
      width: 14px;
      height: 18px;
      animation: walker-bob 0.75s ease-in-out infinite;
      transform-origin: 50% 100%;
    }

    .tag-walker .head {
      position: absolute;
      top: 0;
      left: 50%;
      width: 5px;
      height: 5px;
      margin-left: -2.5px;
      border-radius: 50%;
      background: #ffffff;
    }

    .tag-walker .body {
      position: absolute;
      top: 5px;
      left: 50%;
      width: 2px;
      height: 7px;
      margin-left: -1px;
      border-radius: 2px;
      background: #ffffff;
    }

    .tag-walker .arm {
      position: absolute;
      top: 7px;
      left: 50%;
      width: 2px;
      height: 6px;
      margin-left: -1px;
      border-radius: 2px;
      background: #ffffff;
      transform-origin: 50% 0;
      opacity: 0.95;
    }

    .tag-walker .arm.left {
      animation: arm-swing-left 0.55s ease-in-out infinite;
    }

    .tag-walker .arm.right {
      animation: arm-swing-right 0.55s ease-in-out infinite;
    }

    .tag-walker .leg {
      position: absolute;
      top: 11px;
      left: 50%;
      width: 2px;
      height: 7px;
      margin-left: -1px;
      border-radius: 2px;
      background: #ffffff;
      transform-origin: 50% 0;
      opacity: 0.95;
    }

    .tag-walker .leg.left {
      animation: leg-swing-left 0.55s ease-in-out infinite;
    }

    .tag-walker .leg.right {
      animation: leg-swing-right 0.55s ease-in-out infinite;
    }

    @keyframes walker-bob {
      0%, 100% { transform: translateY(0); }
      50% { transform: translateY(-1px); }
    }

    @keyframes leg-swing-left {
      0%, 100% { transform: rotate(22deg); }
      50% { transform: rotate(-22deg); }
    }

    @keyframes leg-swing-right {
      0%, 100% { transform: rotate(-22deg); }
      50% { transform: rotate(22deg); }
    }

    @keyframes arm-swing-left {
      0%, 100% { transform: rotate(-24deg); }
      50% { transform: rotate(24deg); }
    }

    @keyframes arm-swing-right {
      0%, 100% { transform: rotate(24deg); }
      50% { transform: rotate(-24deg); }
    }

    @keyframes walk-bob {
      0% { transform: translateY(0) rotate(-6deg); }
      25% { transform: translateY(-1px) rotate(3deg); }
      50% { transform: translateY(0) rotate(-2deg); }
      75% { transform: translateY(-1px) rotate(4deg); }
      100% { transform: translateY(0) rotate(-6deg); }
    }

    .locate-btn:hover {
      border-color: rgba(56, 189, 248, 0.35);
      box-shadow: 0 12px 40px rgba(0, 0, 0, 0.55);
    }

    .locate-btn:active {
      transform: scale(0.96);
    }

    .leaflet-control-attribution {
      background: rgba(15, 18, 26, 0.78) !important;
      color: var(--text-muted) !important;
      font-size: 10px !important;
      border-radius: var(--radius-sm) !important;
      border: 1px solid var(--border) !important;
      padding: 4px 8px !important;
      backdrop-filter: blur(8px);
    }

    .leaflet-control-attribution a {
      color: var(--accent) !important;
    }

    /* Zoom +/- bottom-right (below locate / center control) */
    .leaflet-bottom.leaflet-right {
      bottom: max(16px, env(safe-area-inset-bottom)) !important;
      right: max(14px, env(safe-area-inset-right)) !important;
    }

    .leaflet-control-zoom {
      border: none !important;
      box-shadow: var(--shadow-bar) !important;
    }

    .leaflet-bar {
      border: 1px solid var(--border) !important;
      border-radius: var(--radius-md) !important;
      overflow: hidden;
    }

    .leaflet-bar a {
      width: 34px !important;
      height: 34px !important;
      line-height: 34px !important;
      font-size: 18px !important;
      font-weight: 600 !important;
      background: var(--bg-bar) !important;
      color: var(--text) !important;
      border-bottom: 1px solid var(--border) !important;
    }

    .leaflet-bar a:last-child {
      border-bottom: none !important;
    }

    .leaflet-bar a:hover {
      background: var(--bg-elevated) !important;
      color: var(--accent) !important;
    }

    .leaflet-touch .leaflet-bar a {
      width: 36px !important;
      height: 36px !important;
      line-height: 36px !important;
    }
  </style>
</head>
<body>
  <div id="map"></div>

  <div class="ui-overlay" id="ui-overlay">
    <nav class="top-bar" aria-label="Map panels">
      <div class="nav-status-region" id="nav-status-region" aria-live="polite" aria-atomic="true">
        <div class="status" id="status">
          <div class="label">Connecting</div>
          <div>Loading your AirTag location…</div>
        </div>
      </div>
      <div class="top-bar-trailing">
        <button type="button" class="theme-toggle-btn" id="btn-theme"
          title="Light theme — switch to dark map and UI" aria-pressed="false" aria-label="Switch to dark theme">☀️</button>
        <button type="button" class="top-bar-btn" id="btn-timeline"
          aria-expanded="false" aria-controls="sheet-timeline">Timeline</button>
        <span class="refresh-pill" title="Seconds until next refresh">Poll <span class="refresh-timer-num" id="refresh-badge">10</span>s</span>
      </div>
    </nav>

    <div class="sheet" id="sheet-timeline" role="region" aria-label="Timeline" tabindex="-1">
      <div class="timeline-panel">
        <div class="timeline-header">Timeline</div>
        <div class="timeline-controls">
          <button type="button" class="timeline-btn active" id="live-btn">Live</button>
          <button type="button" class="timeline-btn" id="history-btn">History</button>
        </div>
        <label class="live-overlay-toggle" for="live-show-timeline">
          <input type="checkbox" id="live-show-timeline" checked />
          Show timeline for today with live data
        </label>
        <div id="history-mode" style="display:none;">
          <div class="timeline-date-picker-row">
            <label for="timeline-date-picker">Go to date</label>
            <input type="date" id="timeline-date-picker" title="Pick a day (same range as the slider below)" />
          </div>
          <div class="timeline-slider-container">
            <input type="range" min="0" max="100" value="0" class="timeline-slider" id="timeline-slider" />
            <div class="timeline-display">
              <div class="date" id="timeline-date-display">Today</div>
              <div class="days" id="timeline-days-display"></div>
            </div>
            <div class="timeline-loading" id="timeline-loading">Loading history…</div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <div class="details-dock details-dock--open" id="details-dock">
    <div class="details-dock-panel" id="details-dock-panel" role="region" aria-label="Device details" tabindex="-1">
      <div class="info-panel" id="info-panel" style="display:none;">
        <div class="label">Display name</div>
        <div class="info-value" id="device-name-display">--</div>
        <div class="meta-subtle">Apple name: <span id="device-apple-name-display">--</span></div>
        <div class="meta-subtle" id="device-model-display">--</div>

        <div class="details-metrics" aria-label="Device metrics">
          <div class="details-metric-line">
            <span class="details-kv" title="Battery"><span class="details-k">Batt</span> <strong id="battery-display">--</strong></span>
            <span class="details-sep" aria-hidden="true">·</span>
            <span class="details-kv" title="Signal quality"><span class="details-k">Sig</span> <strong id="confidence-display">--</strong></span>
            <span class="details-sep" aria-hidden="true">·</span>
            <span class="details-kv" title="Detections today"><span class="details-k">Today</span> <strong id="detection-day-display">--</strong></span>
            <span class="details-sep" aria-hidden="true">·</span>
            <span class="details-kv" title="Detections all time"><span class="details-k">Saved</span> <strong id="detection-total-display">--</strong></span>
          </div>
          <div class="details-metric-line">
            <span class="details-kv" title="Horizontal accuracy"><span class="details-k">Acc</span> <strong id="accuracy-display">--</strong></span>
            <span class="details-sep" aria-hidden="true">·</span>
            <span class="details-kv" title="Distance today (km, calendar day)"><span class="details-k">km day</span> <strong id="distance-day-display">0.00</strong></span>
            <span class="details-sep" aria-hidden="true">·</span>
            <span class="details-kv" title="Distance total (km, all time)"><span class="details-k">km all</span> <strong id="distance-total-display">0.00</strong></span>
          </div>
        </div>

        <div class="label">Current location</div>
        <div class="info-value" id="coord-display">--</div>

        <div class="label">Last update</div>
        <div class="info-value" id="time-display">--</div>

        <div class="label">Last detected</div>
        <div class="last-detected-box">
          <div class="info-value" id="last-detected-display">--</div>
          <div class="last-detected-elapsed" id="last-detected-elapsed">--</div>
        </div>
      </div>
      <div id="details-placeholder" class="details-placeholder">
        Waiting for first location update…
      </div>
    </div>
    <button type="button" class="details-dock-toggle" id="btn-details"
      title="Hide details" aria-expanded="true" aria-controls="details-dock-panel" aria-label="Hide details">
      <span class="details-toggle-arrow" aria-hidden="true">▼</span>
    </button>
  </div>

  <button type="button" id="locate-tag-btn" class="locate-btn" title="Center on tracker location" aria-label="Center on tracker location"><span class="blue-dot" aria-hidden="true"></span></button>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
    crossorigin=""></script>
  
  <script>
    const map = L.map("map", { zoomControl: false }).setView([0, 0], 2);
    L.control.zoom({ position: "bottomright" }).addTo(map);
    const tileLayerDark = L.tileLayer(
      "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
      {
        attribution:
          '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
        subdomains: "abcd",
        maxZoom: 20,
      }
    );
    const tileLayerLight = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors",
    });
    let baseTileLayer = null;

    function currentUiTheme() {
      return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
    }

    function syncBaseMapLayer() {
      const theme = currentUiTheme();
      const next = theme === "dark" ? tileLayerDark : tileLayerLight;
      if (baseTileLayer && map.hasLayer(baseTileLayer)) {
        map.removeLayer(baseTileLayer);
      }
      baseTileLayer = next;
      baseTileLayer.addTo(map);
    }

    function syncThemeToggleButton() {
      const btn = document.getElementById("btn-theme");
      if (!btn) return;
      const dark = currentUiTheme() === "dark";
      btn.textContent = dark ? "🌙" : "☀️";
      btn.setAttribute("aria-pressed", dark ? "true" : "false");
      btn.title = dark ? "Dark theme — switch to light map and UI" : "Light theme — switch to dark map and UI";
      btn.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
    }

    function applyUiTheme(theme) {
      if (theme !== "light" && theme !== "dark") return;
      document.documentElement.setAttribute("data-theme", theme);
      try {
        localStorage.setItem("ui-theme", theme);
      } catch (e) {}
      syncBaseMapLayer();
      syncThemeToggleButton();
    }

    syncBaseMapLayer();
    syncThemeToggleButton();

    document.getElementById("btn-theme").addEventListener("click", () => {
      applyUiTheme(currentUiTheme() === "dark" ? "light" : "dark");
    });

    let currentMarker = null;
    let polyline = null;
    let locationHistory = [];
    let firstFix = true;
    const refreshIntervalSeconds = 10;
    let refreshCountdown = refreshIntervalSeconds;
    let timelineMode = 'live';  // 'live' or 'history'
    let selectedDate = null;
    /** Set from /api/date-range — slider indices are days offset from first stored date */
    let timelineFirstYMD = null;
    let timelineLastYMD = null;
    const historyMarkersLayer = L.layerGroup().addTo(map);
    let cachedStats = { detection_count_total: 0, distance_total_km: 0, calendar_date_today: '' };
    let livePathRequestId = 0;
    let currentInfluenceCircle = null;
    let currentWaveCircle = null;
    let influencePulseTimer = null;
    let influenceWaveBaseRadius = 60;
    let influenceInnerBaseRadius = 25;
    let influencePulsePhase = 0;
    let timelineSliderDebounceTimer = null;
    let historyLoadRequestId = 0;
    let historyFetchController = null;
    let liveRefreshInFlight = false;
    let lastLiveLocationKey = null;
    let lastLiveRouteHistoryLen = 0;
    const liveShowTimelineToggle = document.getElementById("live-show-timeline");
    const locateTagBtn = document.getElementById("locate-tag-btn");

    const panelButtons = {
      timeline: document.getElementById('btn-timeline'),
    };
    const sheets = {
      timeline: document.getElementById('sheet-timeline'),
    };
    const detailsDock = document.getElementById('details-dock');
    const btnDetails = document.getElementById('btn-details');
    let activePanel = null;

    function syncDetailsDockToggle() {
      const open = detailsDock.classList.contains('details-dock--open');
      const arrow = document.querySelector('.details-toggle-arrow');
      if (arrow) {
        arrow.textContent = open ? '▼' : '▲';
      }
      if (btnDetails) {
        btnDetails.setAttribute('aria-expanded', open ? 'true' : 'false');
        btnDetails.title = open ? 'Hide details' : 'Show details';
        btnDetails.setAttribute('aria-label', open ? 'Hide details panel' : 'Show details panel');
      }
    }

    function toggleDetailsPanel() {
      const open = detailsDock.classList.toggle('details-dock--open');
      syncDetailsDockToggle();
      if (open) {
        try {
          document.getElementById('details-dock-panel').focus({ preventScroll: true });
        } catch (e) {}
      }
      invalidateMapSoon();
    }

    function closeDetailsPanel() {
      detailsDock.classList.remove('details-dock--open');
      syncDetailsDockToggle();
      invalidateMapSoon();
    }

    function invalidateMapSoon() {
      requestAnimationFrame(() => {
        map.invalidateSize();
        setTimeout(() => map.invalidateSize(), 200);
      });
    }

    function closeAllPanels() {
      activePanel = null;
      Object.keys(sheets).forEach((key) => {
        sheets[key].classList.remove('open');
        panelButtons[key].classList.remove('active');
        panelButtons[key].setAttribute('aria-expanded', 'false');
      });
      invalidateMapSoon();
    }

    function openPanel(name) {
      if (!sheets[name]) return;
      if (activePanel === name) {
        closeAllPanels();
        return;
      }
      closeAllPanels();
      activePanel = name;
      sheets[name].classList.add('open');
      panelButtons[name].classList.add('active');
      panelButtons[name].setAttribute('aria-expanded', 'true');
      try {
        sheets[name].focus({ preventScroll: true });
      } catch (e) {}
      invalidateMapSoon();
    }

    Object.keys(panelButtons).forEach((key) => {
      panelButtons[key].addEventListener('click', () => openPanel(key));
    });

    btnDetails.addEventListener('click', () => toggleDetailsPanel());

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        closeAllPanels();
        closeDetailsPanel();
      }
    });

    function hideDetailsPlaceholder() {
      const ph = document.getElementById('details-placeholder');
      if (ph) ph.style.display = 'none';
    }

    function setStatus(msg, isError = false) {
      const el = document.getElementById("status");
      el.innerHTML = msg;
      el.className = isError ? 'status error' : 'status';
    }

    function focusOnTagLocation() {
      if (currentMarker) {
        const ll = currentMarker.getLatLng();
        map.setView([ll.lat, ll.lng], 18, { animate: true });
        try { currentMarker.openPopup(); } catch (e) {}
        return true;
      }
      if (locationHistory.length) {
        const last = locationHistory[locationHistory.length - 1];
        map.setView([last.lat, last.lng], 18, { animate: true });
        return true;
      }
      return false;
    }

    function createAnimatedIcon() {
      return L.divIcon({
        html: '<div style="width:30px;height:30px;border-radius:50%;background:#1f1f1f;border:2px solid #ffffff;display:flex;align-items:center;justify-content:center;box-shadow:0 2px 8px rgba(0,0,0,0.35);"><span class="tag-walker" aria-hidden="true"><span class="head"></span><span class="body"></span><span class="arm left"></span><span class="arm right"></span><span class="leg left"></span><span class="leg right"></span></span></div>',
        iconSize: [30, 30],
        iconAnchor: [15, 15],
        popupAnchor: [0, -16],
        className: 'custom-car-icon'
      });
    }

    function updateCurrentInfluence(lat, lon, accuracyMeters, labelText) {
      const rawAccuracy = Number(accuracyMeters || 25);
      // Inner radius should represent the detection radius directly (do not exceed it).
      const radius = Math.max(8, rawAccuracy);
      const waveRadius = Math.max(radius * 1.08, radius + 6);
      influenceWaveBaseRadius = waveRadius;
      influenceInnerBaseRadius = radius;

      if (!currentInfluenceCircle) {
        currentInfluenceCircle = L.circle([lat, lon], {
          radius,
          color: '#34d399',
          weight: 1.5,
          fillColor: '#34d399',
          fillOpacity: 0.12,
          opacity: 0.45,
        }).addTo(map);
      } else {
        currentInfluenceCircle.setLatLng([lat, lon]);
        currentInfluenceCircle.setRadius(radius);
      }

      if (!currentWaveCircle) {
        currentWaveCircle = L.circle([lat, lon], {
          radius: waveRadius,
          color: '#34d399',
          weight: 1,
          fillOpacity: 0,
          opacity: 0.22,
        }).addTo(map);
      } else {
        currentWaveCircle.setLatLng([lat, lon]);
        currentWaveCircle.setRadius(waveRadius);
      }

      if (!influencePulseTimer) {
        influencePulseTimer = setInterval(() => {
          if (!currentInfluenceCircle || timelineMode !== 'live') return;
          influencePulsePhase += 0.16;
          const pulse = (Math.sin(influencePulsePhase) + 1) / 2; // 0..1
          const animatedInnerRadius = influenceInnerBaseRadius * (1 + pulse * 0.25);
          const animatedInnerOpacity = 0.28 + (1 - pulse) * 0.22;
          currentInfluenceCircle.setRadius(animatedInnerRadius);
          currentInfluenceCircle.setStyle({ opacity: animatedInnerOpacity });

          // Keep outer circle subtle and static (non-animated).
          if (currentWaveCircle) {
            currentWaveCircle.setRadius(influenceWaveBaseRadius);
            currentWaveCircle.setStyle({ opacity: 0.10 });
          }
        }, 120);
      }

      if (currentMarker) {
        currentMarker.unbindTooltip();
        currentMarker.bindTooltip(labelText || 'Current location', {
          permanent: true,
          direction: 'top',
          offset: [0, -18],
          className: 'current-tag-label',
        });
      }
    }

    function calculateDistance(lat1, lon1, lat2, lon2) {
      const R = 6371; // Earth radius in km
      const dLat = (lat2 - lat1) * Math.PI / 180;
      const dLon = (lon2 - lon1) * Math.PI / 180;
      const a = Math.sin(dLat/2) * Math.sin(dLat/2) +
                Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
                Math.sin(dLon/2) * Math.sin(dLon/2);
      const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
      return R * c;
    }

    function calculateTotalDistance(points) {
      let total = 0;
      for (let i = 1; i < points.length; i++) {
        const prev = points[i-1];
        const curr = points[i];
        total += calculateDistance(prev.lat, prev.lng, curr.lat, curr.lng);
      }
      return total;
    }

    function formatElapsedSeconds(totalSeconds) {
      const secs = Math.max(0, Number(totalSeconds || 0));
      const mins = Math.floor(secs / 60);
      const hours = Math.floor(mins / 60);
      const days = Math.floor(hours / 24);

      if (mins < 1) return 'Just now';
      if (mins < 60) return `${mins} minute${mins !== 1 ? 's' : ''} ago`;
      if (hours < 24) return `${hours} hour${hours !== 1 ? 's' : ''} ago`;
      return `${days} day${days !== 1 ? 's' : ''} ago`;
    }

    function localDateYMD(d) {
      const y = d.getFullYear();
      const m = String(d.getMonth() + 1).padStart(2, "0");
      const day = String(d.getDate()).padStart(2, "0");
      return `${y}-${m}-${day}`;
    }

    function getSliderSelectedDateStr() {
      const slider = document.getElementById("timeline-slider");
      const sliderValue = parseInt(slider.value, 10);
      if (timelineFirstYMD) {
        const first = new Date(timelineFirstYMD + "T12:00:00");
        const picked = new Date(first);
        picked.setDate(picked.getDate() + sliderValue);
        return localDateYMD(picked);
      }
      const picked = new Date();
      picked.setHours(12, 0, 0, 0);
      picked.setDate(picked.getDate() - (parseInt(slider.max, 10) - sliderValue));
      return localDateYMD(picked);
    }

    function setSliderFromLocalYMD(ymd) {
      const slider = document.getElementById("timeline-slider");
      const max = parseInt(slider.max, 10) || 0;
      const parts = ymd.split("-").map((x) => parseInt(x, 10));
      if (parts.length !== 3 || parts.some((n) => Number.isNaN(n))) return;
      const target = new Date(parts[0], parts[1] - 1, parts[2], 12, 0, 0, 0);
      if (timelineFirstYMD) {
        const first = new Date(timelineFirstYMD + "T12:00:00");
        const delta = Math.round((target - first) / (1000 * 60 * 60 * 24));
        slider.value = String(Math.max(0, Math.min(max, delta)));
        return;
      }
      const today = new Date();
      today.setHours(12, 0, 0, 0);
      const daysAgo = Math.round((today - target) / (1000 * 60 * 60 * 24));
      let sliderValue = max - daysAgo;
      slider.value = String(Math.max(0, Math.min(max, sliderValue)));
    }

    function updateTimelineDateLabels() {
      const dateDisplay = document.getElementById("timeline-date-display");
      const daysDisplay = document.getElementById("timeline-days-display");
      const dateStr = getSliderSelectedDateStr();
      const pick = new Date(dateStr + "T12:00:00");
      const today = new Date();
      today.setHours(12, 0, 0, 0);
      const daysAgo = Math.round((today - pick) / (1000 * 60 * 60 * 24));

      if (daysAgo === 0) {
        dateDisplay.textContent = "Today";
      } else if (daysAgo === 1) {
        dateDisplay.textContent = "Yesterday";
      } else {
        dateDisplay.textContent = dateStr;
      }

      if (daysAgo > 0) {
        daysDisplay.textContent = `${daysAgo} day${daysAgo !== 1 ? 's' : ''} ago`;
      } else {
        daysDisplay.textContent = '';
      }
      const picker = document.getElementById("timeline-date-picker");
      if (picker) {
        picker.value = dateStr;
      }
      return dateStr;
    }

    function applyLiveStatsFromPayload(data) {
      if (data.display_name !== undefined) {
        document.getElementById("device-name-display").textContent = data.display_name;
      }
      if (data.device_name !== undefined) {
        document.getElementById("device-apple-name-display").textContent = data.device_name;
      }
      if (data.device_model !== undefined) {
        document.getElementById("device-model-display").textContent = data.device_model || "AirTag";
      }
      if (data.detection_count_today !== undefined) {
        document.getElementById("detection-day-display").textContent = String(data.detection_count_today);
      }
      if (data.detection_count_total !== undefined) {
        document.getElementById("detection-total-display").textContent = String(data.detection_count_total);
        cachedStats.detection_count_total = data.detection_count_total;
      }
      if (data.distance_today_km !== undefined) {
        document.getElementById("distance-day-display").textContent = Number(data.distance_today_km).toFixed(2);
      }
      if (data.distance_total_km !== undefined) {
        document.getElementById("distance-total-display").textContent = Number(data.distance_total_km).toFixed(2);
        cachedStats.distance_total_km = data.distance_total_km;
      }
      if (data.calendar_date_today) {
        cachedStats.calendar_date_today = data.calendar_date_today;
      }
    }

    function shouldShowLiveTimelineOverlay() {
      return timelineMode === "live" && !!(liveShowTimelineToggle && liveShowTimelineToggle.checked);
    }

    async function loadBootstrap() {
      try {
        const response = await fetch("/api/bootstrap");
        const data = await response.json();
        if (!response.ok || data.error) return;

        applyLiveStatsFromPayload(data);

        const locs = data.locations_today || [];
        locationHistory = locs.map((e) => ({
          lat: e.latitude,
          lng: e.longitude,
          time: e.timestamp,
        }));

        if (timelineMode !== "live") return;

        if (locs.length) {
          hideDetailsPlaceholder();
          document.getElementById("info-panel").style.display = "block";
        }

        if (locationHistory.length) {
          const last = locationHistory[locationHistory.length - 1];
          if (!currentMarker) {
            currentMarker = L.marker([last.lat, last.lng], { icon: createAnimatedIcon() }).addTo(map);
          } else {
            currentMarker.setLatLng([last.lat, last.lng]);
          }
          const displayName = data.display_name || 'Current location';
          const accMeters = Number((locs[locs.length - 1] && locs[locs.length - 1].accuracy) || 25);
          updateCurrentInfluence(last.lat, last.lng, accMeters, displayName);
          try {
            map.setView([last.lat, last.lng], 15);
            firstFix = false;
          } catch (e) {
            map.setView([last.lat, last.lng], 15);
            firstFix = false;
          }
        }

        if (!shouldShowLiveTimelineOverlay()) {
          if (polyline) {
            map.removeLayer(polyline);
            polyline = null;
          }
          setStatus(`<div class="label">Live mode</div><div>Showing latest detected location only</div>`);
          return;
        }

        const req = ++livePathRequestId;
        if (polyline) {
          map.removeLayer(polyline);
          polyline = null;
        }

        const asLoc = locs.map((e) => ({ latitude: e.latitude, longitude: e.longitude }));
        let routedPathPoints = [];
        if (asLoc.length >= 2) {
          setStatus(`<div class="label">Loading route</div><div>Snapping today's path to roads…</div>`);
          if (Array.isArray(data.snapped_path_today) && data.snapped_path_today.length >= 2) {
            routedPathPoints = data.snapped_path_today;
          } else {
            try {
              const r = await fetch("/api/snap-path", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ locations: asLoc }),
              });
              const j = await r.json();
              if (j.coordinates && j.coordinates.length >= 2) {
                routedPathPoints = j.coordinates;
              } else {
                routedPathPoints = await getRoutedPath(asLoc);
              }
            } catch (e) {
              routedPathPoints = await getRoutedPath(asLoc);
            }
          }
          if (req !== livePathRequestId) return;
          setStatus(`<div class="label">Ready</div><div>Loaded saved track for today</div>`);
        }
        if (req !== livePathRequestId) return;

        if (routedPathPoints.length >= 2) {
          polyline = L.polyline(routedPathPoints, {
            color: "#34d399",
            weight: 4,
            opacity: 0.78,
            lineCap: "round",
            lineJoin: "round",
          }).addTo(map);
          try {
            map.fitBounds(polyline.getBounds(), { padding: [40, 40] });
          } catch (e) {}
        }
      } catch (e) {
        console.warn("bootstrap failed:", e);
      }
    }

    async function drawLiveRoutedPath() {
      if (!shouldShowLiveTimelineOverlay()) {
        if (polyline) {
          map.removeLayer(polyline);
          polyline = null;
        }
        return;
      }
      const req = ++livePathRequestId;
      if (polyline) {
        map.removeLayer(polyline);
        polyline = null;
      }
      const asLoc = locationHistory.map((p) => ({ latitude: p.lat, longitude: p.lng }));
      if (asLoc.length < 2) return;
      let routedPathPoints = [];
      try {
        const r = await fetch("/api/snap-path", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ locations: asLoc }),
        });
        const j = await r.json();
        if (j.coordinates && j.coordinates.length >= 2) {
          routedPathPoints = j.coordinates;
        }
      } catch (e) {
        console.warn("server snap-path failed, falling back:", e);
      }
      if (!routedPathPoints.length) {
        routedPathPoints = await getRoutedPath(asLoc);
      }
      if (req !== livePathRequestId) return;
      if (routedPathPoints.length >= 2) {
        polyline = L.polyline(routedPathPoints, {
          color: "#34d399",
          weight: 4,
          opacity: 0.78,
          lineCap: "round",
          lineJoin: "round",
        }).addTo(map);
      }
    }

    async function loadDateRange() {
      try {
        const response = await fetch("/api/date-range");
        const data = await response.json();

        if (data.first && data.last) {
          timelineFirstYMD = data.first;
          timelineLastYMD = data.last;
          const firstDate = new Date(data.first + "T12:00:00");
          const lastDate = new Date(data.last + "T12:00:00");
          const daysDiff = Math.max(
            0,
            Math.floor((lastDate - firstDate) / (1000 * 60 * 60 * 24)),
          );

          const slider = document.getElementById("timeline-slider");
          slider.max = String(daysDiff);
          slider.value = String(daysDiff);
          const picker = document.getElementById("timeline-date-picker");
          if (picker) {
            picker.min = data.first;
            picker.max = data.last;
          }
          updateTimelineDateLabels();
        } else {
          timelineFirstYMD = null;
          timelineLastYMD = null;
          const picker = document.getElementById("timeline-date-picker");
          if (picker) {
            picker.removeAttribute("min");
            picker.removeAttribute("max");
            picker.value = "";
          }
        }
      } catch (error) {
        console.error("Error loading date range:", error);
      }
    }

    function updateSliderDisplay() {
      updateTimelineDateLabels();
      if (timelineMode === "history") {
        if (timelineSliderDebounceTimer) {
          clearTimeout(timelineSliderDebounceTimer);
        }
        timelineSliderDebounceTimer = setTimeout(() => {
          loadTimelineData(getSliderSelectedDateStr());
        }, 220);
      }
    }

    function setTimelineLoading(active, message) {
      const el = document.getElementById("timeline-loading");
      if (!el) return;
      if (message) {
        el.textContent = message;
      } else if (active) {
        el.textContent = "Loading history…";
      }
      el.classList.toggle("active", !!active);
    }

    function coordsClose(a, b) {
      return Math.abs(a[0] - b[0]) < 1e-7 && Math.abs(a[1] - b[1]) < 1e-7;
    }

    /** OSRM match often returns several matchings when the trace has gaps (sparse AirTag pings). We must merge all of them. */
    function mergeMatchingGeometries(data) {
      const out = [];
      if (!data.matchings || !data.matchings.length) return out;
      for (const m of data.matchings) {
        if (!m.geometry || !m.geometry.coordinates || !m.geometry.coordinates.length) continue;
        const seg = m.geometry.coordinates.map((c) => [c[1], c[0]]);
        if (!out.length) {
          out.push(...seg);
        } else if (seg.length) {
          const last = out[out.length - 1];
          const first = seg[0];
          if (coordsClose(last, first)) out.push(...seg.slice(1));
          else out.push(...seg);
        }
      }
      return out;
    }

    async function routeStraightChunk(chunk) {
      const coordStr = chunk.map((loc) => `${loc.longitude},${loc.latitude}`).join(";");
      const url = `https://router.project-osrm.org/route/v1/driving/${coordStr}?geometries=geojson&overview=full`;
      const response = await fetch(url);
      if (!response.ok) return [];
      const data = await response.json();
      if (data.routes && data.routes.length && data.routes[0].geometry && data.routes[0].geometry.coordinates) {
        return data.routes[0].geometry.coordinates.map((c) => [c[1], c[0]]);
      }
      return [];
    }

    /** Visit each consecutive pair — road-snaps sparse legs when match/route multi-waypoint fails. */
    async function routePairwise(chunk) {
      const merged = [];
      for (let i = 0; i < chunk.length - 1; i++) {
        const a = chunk[i];
        const b = chunk[i + 1];
        const url =
          `https://router.project-osrm.org/route/v1/driving/${a.longitude},${a.latitude};${b.longitude},${b.latitude}` +
          `?geometries=geojson&overview=full`;
        try {
          const response = await fetch(url);
          if (!response.ok) throw new Error(String(response.status));
          const data = await response.json();
          const route = data.routes && data.routes[0];
          if (route && route.geometry && route.geometry.coordinates && route.geometry.coordinates.length) {
            const seg = route.geometry.coordinates.map((c) => [c[1], c[0]]);
            if (!merged.length) {
              merged.push(...seg);
            } else if (seg.length) {
              const last = merged[merged.length - 1];
              const first = seg[0];
              if (coordsClose(last, first)) merged.push(...seg.slice(1));
              else merged.push(...seg);
            }
          } else {
            const fallback = [[a.latitude, a.longitude], [b.latitude, b.longitude]];
            if (!merged.length) merged.push(...fallback);
            else {
              const last = merged[merged.length - 1];
              const first = fallback[0];
              if (coordsClose(last, first)) merged.push(...fallback.slice(1));
              else merged.push(...fallback);
            }
          }
        } catch (e) {
          const fallback = [[a.latitude, a.longitude], [b.latitude, b.longitude]];
          if (!merged.length) merged.push(...fallback);
          else {
            const last = merged[merged.length - 1];
            const first = fallback[0];
            if (coordsClose(last, first)) merged.push(...fallback.slice(1));
            else merged.push(...fallback);
          }
        }
      }
      return merged;
    }

    async function matchChunk(chunk) {
      const coordStr = chunk.map((loc) => `${loc.longitude},${loc.latitude}`).join(";");
      const radiusM = 75;
      const radiuses = chunk.map(() => String(radiusM)).join(";");
      const url =
        `https://router.project-osrm.org/match/v1/driving/${coordStr}` +
        `?geometries=geojson&overview=full&radiuses=${radiuses}`;
      let part = [];
      try {
        const response = await fetch(url);
        if (response.ok) {
          const data = await response.json();
          part = mergeMatchingGeometries(data);
          if (!part.length && data.routes && data.routes.length && data.routes[0].geometry) {
            part = data.routes[0].geometry.coordinates.map((c) => [c[1], c[0]]);
          }
        }
      } catch (e) {
        console.warn("OSRM match chunk failed:", e);
      }
      if (!part.length) {
        part = await routeStraightChunk(chunk);
      }
      /* Pairwise routing snaps sparse legs but is N−1 requests — only for smaller chunks. */
      if (!part.length && chunk.length <= 30) {
        part = await routePairwise(chunk);
      }
      if (!part.length) {
        part = chunk.map((loc) => [loc.latitude, loc.longitude]);
      }
      return part;
    }

    async function getRoutedPath(locations) {
      if (!locations || locations.length === 0) return [];
      if (locations.length === 1) {
        const loc = locations[0];
        return [[loc.latitude, loc.longitude]];
      }

      const maxChunk = 85;
      const step = maxChunk - 1;
      const merged = [];

      try {
        for (let start = 0; start < locations.length; start += step) {
          const chunk = locations.slice(start, Math.min(start + maxChunk, locations.length));
          if (chunk.length === 1) {
            const only = chunk[0];
            const pt = [only.latitude, only.longitude];
            if (!merged.length || !coordsClose(merged[merged.length - 1], pt)) merged.push(pt);
            continue;
          }
          const part = await matchChunk(chunk);
          if (!merged.length) {
            merged.push(...part);
          } else if (part.length) {
            const first = part[0];
            const last = merged[merged.length - 1];
            if (coordsClose(last, first)) merged.push(...part.slice(1));
            else merged.push(...part);
          }
        }
        return merged.length ? merged : locations.map((loc) => [loc.latitude, loc.longitude]);
      } catch (err) {
        console.warn("Road match failed, using straight segments:", err);
        return locations.map((loc) => [loc.latitude, loc.longitude]);
      }
    }

    async function loadTimelineData(date) {
      const reqId = ++historyLoadRequestId;
      if (historyFetchController) {
        try { historyFetchController.abort(); } catch (e) {}
      }
      historyFetchController = new AbortController();
      setTimelineLoading(true, "Loading history and road snap…");
      try {
        const response = await fetch(`/api/history?date=${date}`, { signal: historyFetchController.signal });
        const data = await response.json();
        if (reqId !== historyLoadRequestId) return;

        if (!data.locations || data.locations.length === 0) {
          setStatus(`<div class="label">No data</div><div>No location data for ${date}</div>`);
          setTimelineLoading(false);
          return;
        }

        timelineMode = 'history';
        selectedDate = date;

        if (polyline) {
          map.removeLayer(polyline);
          polyline = null;
        }
        if (currentMarker) {
          map.removeLayer(currentMarker);
          currentMarker = null;
        }
        if (currentInfluenceCircle) {
          map.removeLayer(currentInfluenceCircle);
          currentInfluenceCircle = null;
        }
        if (currentWaveCircle) {
          map.removeLayer(currentWaveCircle);
          currentWaveCircle = null;
        }
        if (influencePulseTimer) {
          clearInterval(influencePulseTimer);
          influencePulseTimer = null;
        }
        historyMarkersLayer.clearLayers();

        setStatus(`<div class="label">Loading route</div><div>Snapping path to roads…</div>`);
        let routedPathPoints = [];
        if (Array.isArray(data.snapped_path) && data.snapped_path.length >= 2) {
          routedPathPoints = data.snapped_path;
        } else {
          try {
            const r = await fetch("/api/snap-path", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ locations: data.locations }),
            });
            const j = await r.json();
            if (reqId !== historyLoadRequestId) return;
            if (j.coordinates && j.coordinates.length >= 2) {
              routedPathPoints = j.coordinates;
            } else {
              routedPathPoints = await getRoutedPath(data.locations);
            }
          } catch (e) {
            routedPathPoints = await getRoutedPath(data.locations);
          }
        }
        if (reqId !== historyLoadRequestId) return;

        if (!Array.isArray(routedPathPoints) || routedPathPoints.length === 0) {
          routedPathPoints = data.locations.map((loc) => [loc.latitude, loc.longitude]);
        }

        if (routedPathPoints.length > 0) {
          polyline = L.polyline(routedPathPoints, {
            color: '#38bdf8',
            weight: 4,
            opacity: 0.88,
            lineCap: 'round',
            lineJoin: 'round'
          }).addTo(map);
        }

        const markerStride = data.locations.length > 400 ? Math.ceil(data.locations.length / 220) : 1;
        data.locations.forEach((loc, idx) => {
          if (idx !== data.locations.length - 1 && idx % markerStride !== 0) {
            return;
          }
          const markerColor = idx === data.locations.length - 1 ? '#f59e0b' : '#38bdf8';
          L.circleMarker([loc.latitude, loc.longitude], {
            radius: idx === data.locations.length - 1 ? 8 : 5,
            fillColor: markerColor,
            color: 'white',
            weight: 2,
            opacity: 0.8,
            fillOpacity: 0.7
          }).bindPopup(
            `<b>Detection ${idx + 1}</b><br/>` +
            `${loc.latitude.toFixed(6)}, ${loc.longitude.toFixed(6)}<br/>` +
            `Accuracy: ±${loc.accuracy}m<br/>` +
            `${loc.timestamp}`
          ).addTo(historyMarkersLayer);
        });

        const lastLoc = data.locations[data.locations.length - 1];
        currentMarker = L.marker(
          [lastLoc.latitude, lastLoc.longitude],
          { icon: createAnimatedIcon() }
        ).addTo(map);
        updateCurrentInfluence(
          lastLoc.latitude,
          lastLoc.longitude,
          Number(lastLoc.accuracy || 25),
          document.getElementById("device-name-display").textContent || 'Current location'
        );

        currentMarker.bindPopup(
          `<b>${date}</b><br/>` +
          `${lastLoc.latitude.toFixed(6)}, ${lastLoc.longitude.toFixed(6)}<br/>` +
          `Accuracy: ±${lastLoc.accuracy}m<br/>` +
          `${lastLoc.timestamp}`
        );

        if (polyline) {
          try {
            map.fitBounds(polyline.getBounds(), { padding: [50, 50] });
          } catch (e) {
            map.setView([lastLoc.latitude, lastLoc.longitude], 15);
          }
        } else if (data.locations.length > 0) {
          map.setView([lastLoc.latitude, lastLoc.longitude], 15);
        }

        const infoPanel = document.getElementById("info-panel");
        infoPanel.style.display = 'block';
        hideDetailsPlaceholder();
        document.getElementById("coord-display").textContent =
          `${lastLoc.latitude.toFixed(6)}, ${lastLoc.longitude.toFixed(6)}`;
        document.getElementById("accuracy-display").textContent =
          `±${lastLoc.accuracy}m`;
        document.getElementById("time-display").textContent = date;

        const dayKm = data.distance_day_km !== undefined
          ? Number(data.distance_day_km)
          : calculateTotalDistance(data.locations.map((l) => ({
              lat: l.latitude,
              lng: l.longitude
            })));
        document.getElementById("distance-day-display").textContent = dayKm.toFixed(2);
        document.getElementById("distance-total-display").textContent =
          Number(cachedStats.distance_total_km || 0).toFixed(2);

        const dayCount = data.total_count !== undefined ? data.total_count : data.count;
        document.getElementById("detection-day-display").textContent = String(dayCount);
        document.getElementById("detection-total-display").textContent =
          String(cachedStats.detection_count_total);

        document.getElementById("last-detected-display").textContent = lastLoc.timestamp;
        document.getElementById("last-detected-elapsed").textContent = 'Last location on this date';

        setStatus(
          `<div class="label">Historical view</div>` +
          `<div>${date}: ${dayCount} saved points${data.sampled ? " (sampled for map)" : ""}</div>` +
          `<div class="status-hint">Select Live in Timeline to return</div>`
        );
        setTimelineLoading(false);
      } catch (error) {
        if (error && error.name === "AbortError") return;
        setStatus(`<div class="label">Error</div><div>${error.message}</div>`, true);
        setTimelineLoading(false, "Failed to load history");
      } finally {
        if (historyFetchController && reqId === historyLoadRequestId) {
          historyFetchController = null;
        }
      }
    }

    async function refreshLocation() {
      if (timelineMode === 'history') return;
      if (liveRefreshInFlight) return;
      liveRefreshInFlight = true;
      let timeoutId = null;

      try {
        const controller = new AbortController();
        timeoutId = setTimeout(() => controller.abort(), 15000);
        const response = await fetch("/api/location", { signal: controller.signal });
        clearTimeout(timeoutId);
        timeoutId = null;
        const data = await response.json();

        if (!response.ok) {
          setStatus(`<div class="label">Error</div><div>${data.error || "Unknown error"}</div>`, true);
          return;
        }

        if (!data.has_location) {
          setStatus(`<div class="label">Waiting</div><div>No location report available yet</div>`);
          return;
        }

        const lat = data.latitude;
        const lon = data.longitude;
        const updated = data.timestamp_local || "unknown";
        const acc = data.horizontal_accuracy;
        const liveLocationKey = `${data.timestamp_iso || ''}|${lat.toFixed(6)}|${lon.toFixed(6)}`;
        const isSameLiveFix = lastLiveLocationKey === liveLocationKey;

        if (locationHistory.length === 0 ||
            (Math.abs(locationHistory[locationHistory.length - 1].lat - lat) > 0.0001 ||
             Math.abs(locationHistory[locationHistory.length - 1].lng - lon) > 0.0001)) {
          locationHistory.push({ lat, lng: lon, time: updated });
        }

        if (!currentMarker) {
          currentMarker = L.marker([lat, lon], { icon: createAnimatedIcon() }).addTo(map);
        } else {
          currentMarker.setLatLng([lat, lon]);
        }
        updateCurrentInfluence(
          lat,
          lon,
          Number(acc || 25),
          data.display_name || data.device_name || 'Current location'
        );

        currentMarker.bindPopup(
          `<b>Current location</b><br/>` +
          `<strong>${lat.toFixed(6)}, ${lon.toFixed(6)}</strong><br/>` +
          `Accuracy: ±${acc}m<br/>` +
          `${updated}`
        );

        const infoPanel = document.getElementById("info-panel");
        infoPanel.style.display = 'block';
        hideDetailsPlaceholder();

        applyLiveStatsFromPayload(data);

        if (data.battery_text) {
          document.getElementById("battery-display").textContent = data.battery_text;
        } else if (data.battery_percent !== undefined) {
          document.getElementById("battery-display").textContent = `${data.battery_percent}%`;
        }
        if (data.confidence_text) {
          document.getElementById("confidence-display").textContent = data.confidence_text;
        }

        document.getElementById("coord-display").textContent =
          `${lat.toFixed(6)}, ${lon.toFixed(6)}`;
        document.getElementById("accuracy-display").textContent =
          `±${acc}m`;
        document.getElementById("time-display").textContent = updated;

        const detectedTime = new Date(data.timestamp_iso);
        const elapsedText = formatElapsedSeconds(
          data.report_age_seconds !== undefined
            ? data.report_age_seconds
            : Math.floor((Date.now() - detectedTime.getTime()) / 1000)
        );

        document.getElementById("last-detected-display").textContent = detectedTime.toLocaleString();
        document.getElementById("last-detected-elapsed").textContent = elapsedText;

        if (shouldShowLiveTimelineOverlay() && (!isSameLiveFix || locationHistory.length !== lastLiveRouteHistoryLen)) {
          await drawLiveRoutedPath();
          lastLiveRouteHistoryLen = locationHistory.length;
        }
        lastLiveLocationKey = liveLocationKey;

        if (firstFix) {
          map.setView([lat, lon], 17);
          firstFix = false;
        }

        const reportAgeSeconds = Number(data.report_age_seconds || 0);
        const staleHint = reportAgeSeconds >= 3600
          ? `<div class="status-hint status-hint--warn">Latest Find My report is ${formatElapsedSeconds(reportAgeSeconds)} (network delay is possible)</div>`
          : '';
        setStatus(
          `<div class="label">Live tracking</div>` +
          `<div>${lat.toFixed(6)}, ${lon.toFixed(6)}</div>` +
          `<div class="status-hint">±${acc}m accuracy</div>` +
          staleHint
        );

        refreshCountdown = refreshIntervalSeconds;
      } catch (error) {
        const msg = (error && error.name === "AbortError")
          ? "Location request timed out. Retrying..."
          : (error && error.message) ? error.message : "Unknown network error";
        setStatus(`<div class="label">Connection error</div><div>${msg}</div>`, true);
      } finally {
        if (timeoutId) clearTimeout(timeoutId);
        liveRefreshInFlight = false;
      }
    }

    // Refresh countdown
    setInterval(() => {
      refreshCountdown--;
      if (refreshCountdown <= 0) {
        refreshCountdown = refreshIntervalSeconds;
        refreshLocation();
      }
      document.getElementById("refresh-badge").textContent = String(refreshCountdown);
    }, 1000);

    document.getElementById("live-btn").onclick = async () => {
      historyLoadRequestId++;
      if (historyFetchController) {
        try { historyFetchController.abort(); } catch (e) {}
        historyFetchController = null;
      }
      setTimelineLoading(false);
      timelineMode = 'live';
      historyMarkersLayer.clearLayers();
      if (polyline) {
        map.removeLayer(polyline);
        polyline = null;
      }
      if (currentMarker) {
        map.removeLayer(currentMarker);
        currentMarker = null;
      }
      if (currentInfluenceCircle) {
        map.removeLayer(currentInfluenceCircle);
        currentInfluenceCircle = null;
      }
      if (currentWaveCircle) {
        map.removeLayer(currentWaveCircle);
        currentWaveCircle = null;
      }
      if (influencePulseTimer) {
        clearInterval(influencePulseTimer);
        influencePulseTimer = null;
      }
      selectedDate = null;
      firstFix = true;
      document.getElementById("history-mode").style.display = 'none';
      document.getElementById("live-btn").classList.add('active');
      document.getElementById("history-btn").classList.remove('active');
      await loadBootstrap();
      refreshLocation();
    };

    document.getElementById("history-btn").onclick = () => {
      document.getElementById("history-mode").style.display = 'block';
      document.getElementById("history-btn").classList.add('active');
      document.getElementById("live-btn").classList.remove('active');
      timelineMode = 'history';
      setTimelineLoading(true, "Loading history and road snap…");
      updateSliderDisplay();
    };

    document.getElementById("timeline-slider").addEventListener('input', updateSliderDisplay);

    document.getElementById("timeline-date-picker").addEventListener("change", () => {
      const picker = document.getElementById("timeline-date-picker");
      const ymd = picker.value;
      if (!ymd) return;
      const mn = picker.min;
      const mx = picker.max;
      if (mn && ymd < mn) picker.value = mn;
      if (mx && ymd > mx) picker.value = mx;
      const useYmd = picker.value || ymd;
      setSliderFromLocalYMD(useYmd);
      updateTimelineDateLabels();
      if (timelineMode === "history") {
        if (timelineSliderDebounceTimer) {
          clearTimeout(timelineSliderDebounceTimer);
        }
        loadTimelineData(getSliderSelectedDateStr());
      }
    });
    locateTagBtn.addEventListener('click', () => {
      if (!focusOnTagLocation()) {
        setStatus(`<div class="label">Waiting</div><div>No saved location yet to center on</div>`);
      }
    });
    liveShowTimelineToggle.addEventListener('change', async () => {
      if (timelineMode !== "live") return;
      if (liveShowTimelineToggle.checked) {
        await drawLiveRoutedPath();
        setStatus(`<div class="label">Live tracking</div><div>Live location and today's path</div>`);
      } else {
        if (polyline) {
          map.removeLayer(polyline);
          polyline = null;
        }
        setStatus(`<div class="label">Live mode</div><div>Showing latest detected location only</div>`);
      }
    });

    (async () => {
      await loadDateRange();
      await loadBootstrap();
      refreshLocation();
    })();
  </script>
</body>
</html>
"""


def _is_port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _single_instance_lock_path() -> Path:
    return Path(__file__).resolve().parent / "web_tracker_app.lock"


def _acquire_single_instance(lock_path: Path) -> tuple[object | None, bool]:
    """Try exclusive flock on lock_path. Caller must keep fd open until exit."""
    try:
        import fcntl as _fcntl
    except ImportError:
        return None, True  # e.g. Windows: skip lock rather than crashing

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115 — held for process lifetime
    try:
        _fcntl.flock(fd.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except BlockingIOError:
        fd.close()
        return None, False
    try:
        fd.seek(0)
        fd.truncate()
        fd.write(str(os.getpid()))
        fd.flush()
    except OSError:
        pass
    return fd, True


def _find_available_port(host: str, preferred_port: int, max_tries: int = 200) -> int:
    if preferred_port == 0:
        # Let the OS allocate a free ephemeral port.
        return 0

    if _is_port_available(host, preferred_port):
        return preferred_port

    for candidate in range(preferred_port + 1, preferred_port + max_tries + 1):
        if _is_port_available(host, candidate):
            return candidate

    msg = f"Could not find a free port in range {preferred_port}-{preferred_port + max_tries}"
    raise RuntimeError(msg)


def create_app(account, tracker: KeyPair | FindMyAccessory) -> web.Application:
    app = web.Application()
    
    # Initialize location history database
    history_db = LocationHistoryDB(LOCATION_HISTORY_PATH)
    tracker_config = load_tracker_config(TRACKER_CONFIG_PATH)
    snap_cache_holder: dict = {"data": load_snap_cache()}
    # Anisette/account fetch calls are not re-entrant; serialize API fetches.
    account_fetch_lock = asyncio.Lock()

    async def on_http_startup(a: web.Application) -> None:
        a["http_session"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        snap_cache_holder["data"] = load_snap_cache()

    async def on_http_cleanup(a: web.Application) -> None:
        await a["http_session"].close()

    app.on_startup.append(on_http_startup)
    app.on_cleanup.append(on_http_cleanup)

    def calendar_today_str() -> str:
        return datetime.now(APP_TIMEZONE).strftime("%Y-%m-%d")

    def stats_from_db() -> dict:
        """Distance and detection counts derived from persisted location_history.json."""
        today = calendar_today_str()
        today_entries = history_db.get_by_date(today)
        ordered_all = sorted(history_db.history, key=lambda e: e.get("timestamp") or "")
        return {
            "calendar_date_today": today,
            "detection_count_today": len(today_entries),
            "detection_count_total": len(history_db.history),
            "distance_today_km": distance_along_entries(today_entries),
            "distance_total_km": distance_along_entries(ordered_all),
        }

    def display_name_for_tracker() -> str:
        custom = (tracker_config.get("display_name") or "").strip()
        if custom:
            return custom
        return getattr(tracker, "name", "Unknown AirTag") or "Unknown AirTag"

    async def index(_: web.Request) -> web.Response:
        return web.Response(
            text=HTML_PAGE,
            content_type="text/html",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    def get_battery_text(status_byte: int) -> str:
        """Extract battery state from status byte."""
        battery_id = (status_byte >> 6) & 0b11
        battery_map = {
            0b00: "Full",
            0b01: "Medium",
            0b10: "Low",
            0b11: "Very Low",
        }
        return battery_map.get(battery_id, "Unknown")

    def get_confidence_text(confidence: int) -> str:
        """Convert confidence level (1-3) to text"""
        confidence_map = {1: "Low", 2: "Medium", 3: "High"}
        label = confidence_map.get(confidence)
        if label is not None:
            return label
        return f"Unknown ({confidence})"

    async def api_location(_: web.Request) -> web.Response:
        try:
            logger.info("Fetching location for tracker...")
            async with account_fetch_lock:
                location = await account.fetch_location(tracker)
            logger.info(f"Fetch result: {location}")
            account.to_json(STORE_PATH)
        except Exception as exc:
            logger.error(f"Error fetching location: {exc}", exc_info=True)
            return web.json_response({"error": str(exc), "has_location": False}, status=500)

        if location is None:
            logger.warning("No location report available - tracker may be offline or not reporting yet")
            return web.json_response({"has_location": False})

        # Convert UTC timestamp to EST
        est_tz = ZoneInfo("America/New_York")
        est_time = location.timestamp.astimezone(est_tz)
        
        # Save to history database with EST timestamp
        timestamp_str = est_time.strftime("%Y-%m-%d %H:%M:%S %Z")
        history_db.add(location.latitude, location.longitude, location.horizontal_accuracy, timestamp_str)

        stats = stats_from_db()

        # Extract device information (Apple name vs user display name)
        device_name = getattr(tracker, 'name', 'Unknown AirTag')
        device_model = getattr(tracker, 'model', 'AirTag')
        
        # Extract battery state from status byte
        battery_text = get_battery_text(location.status)
        
        # Get confidence text
        confidence_text = get_confidence_text(location.confidence)
        
        report_age_seconds = max(
            0,
            int(
                (
                    datetime.now(timezone.utc)
                    - location.timestamp.astimezone(timezone.utc)
                ).total_seconds()
            ),
        )

        payload = {
            "has_location": True,
            "device_name": device_name,
            "device_model": device_model,
            "display_name": display_name_for_tracker(),
            "battery_text": battery_text,
            "detection_count": stats["detection_count_today"],
            "detection_count_today": stats["detection_count_today"],
            "detection_count_total": stats["detection_count_total"],
            "distance_today_km": stats["distance_today_km"],
            "distance_total_km": stats["distance_total_km"],
            "calendar_date_today": stats["calendar_date_today"],
            "latitude": location.latitude,
            "longitude": location.longitude,
            "horizontal_accuracy": location.horizontal_accuracy,
            "confidence": location.confidence,
            "confidence_text": confidence_text,
            "timestamp_iso": location.timestamp.isoformat(),
            "timestamp_local": est_time.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "report_age_seconds": report_age_seconds,
            "server_time": datetime.now().astimezone().isoformat(),
        }
        return web.Response(
            text=json.dumps(payload),
            content_type="application/json",
        )

    async def api_date_range(_: web.Request) -> web.Response:
        """Get the date range of available location data"""
        try:
            date_range = history_db.get_date_range()
            return web.json_response(date_range)
        except Exception as exc:
            logger.error(f"Error getting date range: {exc}")
            return web.json_response({"error": str(exc)}, status=500)

    async def api_history(request: web.Request) -> web.Response:
        """Get location history for a specific date (includes server-side road-snapped path + cache)."""
        try:
            date = request.query.get('date')
            if not date:
                return web.json_response({"error": "date parameter required"}, status=400)

            locations = history_db.get_by_date(date)
            if not locations:
                return web.json_response({"locations": []})

            ordered = sorted(locations, key=lambda e: e.get("timestamp") or "")
            day_distance_km = distance_along_entries(ordered)
            render_locations = downsample_entries_uniform(ordered, TIMELINE_RENDER_MAX_POINTS)
            snap_input = downsample_entries_uniform(render_locations, TIMELINE_SNAP_MAX_POINTS)
            full_fp = fingerprint_entries(ordered)
            snap_fp = fingerprint_entries(snap_input)
            cache = snap_cache_holder["data"].get(date)
            response_payload: dict | None = None
            if isinstance(cache, dict):
                # New format: full response payload persisted by full day fingerprint.
                if cache.get("full_fingerprint") == full_fp and isinstance(cache.get("payload"), dict):
                    response_payload = cache["payload"]
                    logger.info("History cache hit for %s (%s points)", date, len(render_locations))
                # Backward-compatible: older cache files only had snapped coordinates by one fingerprint key.
                elif cache.get("fingerprint") == snap_fp and cache.get("coordinates"):
                    response_payload = {
                        "date": date,
                        "count": len(render_locations),
                        "total_count": len(ordered),
                        "sampled": len(render_locations) != len(ordered),
                        "locations": render_locations,
                        "distance_day_km": day_distance_km,
                        "snapped_path": cache["coordinates"],
                    }
                    logger.info("Road snap legacy cache hit for %s (%s points)", date, len(snap_input))

            if response_payload is not None:
                return web.json_response(response_payload)

            snapped_path: list | None = None
            if len(snap_input) >= 2:
                session = request.app["http_session"]
                snapped_path = await snap_locations_to_roads(session, snap_input)
                logger.info("Road snap computed for %s (%s points)", date, len(snap_input))
            else:
                snapped_path = []

            payload = {
                "date": date,
                "count": len(render_locations),
                "total_count": len(ordered),
                "sampled": len(render_locations) != len(ordered),
                "locations": render_locations,
                "distance_day_km": day_distance_km,
                "snapped_path": snapped_path,
            }
            snap_cache_holder["data"][date] = {
                "full_fingerprint": full_fp,
                "snap_fingerprint": snap_fp,
                "payload": payload,
            }
            save_snap_cache(snap_cache_holder["data"])

            return web.json_response(payload)
        except Exception as exc:
            logger.error(f"Error fetching history: {exc}")
            return web.json_response({"error": str(exc)}, status=500)

    async def api_bootstrap(request: web.Request) -> web.Response:
        """Initial session data: today's points, stats, names (survives page refresh)."""
        try:
            stats = stats_from_db()
            today = stats["calendar_date_today"]
            locations_today = sorted(
                history_db.get_by_date(today),
                key=lambda e: e.get("timestamp") or "",
            )
            snapped_today: list | None = None
            if len(locations_today) >= 2:
                fp = fingerprint_entries(locations_today)
                cache = snap_cache_holder["data"].get(today)
                if isinstance(cache, dict) and cache.get("fingerprint") == fp and cache.get("coordinates"):
                    snapped_today = cache["coordinates"]
                else:
                    session = request.app["http_session"]
                    snapped_today = await snap_locations_to_roads(session, locations_today)
                    snap_cache_holder["data"][today] = {"fingerprint": fp, "coordinates": snapped_today}
                    save_snap_cache(snap_cache_holder["data"])

            payload = {
                **stats,
                "locations_today": locations_today,
                "snapped_path_today": snapped_today or [],
                "device_name": getattr(tracker, "name", "Unknown AirTag"),
                "device_model": getattr(tracker, "model", "AirTag"),
                "display_name": display_name_for_tracker(),
            }
            return web.json_response(payload)
        except Exception as exc:
            logger.error("bootstrap failed: %s", exc, exc_info=True)
            return web.json_response({"error": str(exc)}, status=500)

    async def api_snap_path(request: web.Request) -> web.Response:
        """Live map: road-snap arbitrary location list (server-side OSRM; no browser CORS/rate quirks)."""
        try:
            body = await request.json()
            locs = body.get("locations") or []
            if len(locs) < 2:
                return web.json_response({"coordinates": []})
            session = request.app["http_session"]
            snapped = await snap_locations_to_roads(session, locs)
            return web.json_response({"coordinates": snapped})
        except Exception as exc:
            logger.error("snap-path: %s", exc, exc_info=True)
            return web.json_response({"error": str(exc), "coordinates": []}, status=500)

    async def api_location_history(_: web.Request) -> web.Response:
        """Endpoint to fetch location history for debugging"""
        try:
            logger.info("Fetching location history for tracker...")
            async with account_fetch_lock:
                history = await account.fetch_location_history(tracker)
            logger.info(f"History fetch result: {len(history)} reports" if isinstance(history, list) else f"History: {history}")
            
            if isinstance(history, list):
                reports = [
                    {
                        "latitude": loc.latitude,
                        "longitude": loc.longitude,
                        "accuracy": loc.horizontal_accuracy,
                        "timestamp": loc.timestamp.isoformat(),
                    }
                    for loc in history
                ]
                return web.json_response({"count": len(reports), "reports": reports})
            else:
                return web.json_response({"error": "Unexpected response type", "history": str(history)}, status=500)
        except Exception as exc:
            logger.error(f"Error fetching history: {exc}", exc_info=True)
            return web.json_response({"error": str(exc)}, status=500)

    app.router.add_get("/", index)
    app.router.add_get("/api/location", api_location)
    app.router.add_get("/api/date-range", api_date_range)
    app.router.add_get("/api/history", api_history)
    app.router.add_get("/api/bootstrap", api_bootstrap)
    app.router.add_post("/api/snap-path", api_snap_path)
    app.router.add_get("/api/location-history", api_location_history)
    return app


async def init_app(tracker: KeyPair | FindMyAccessory) -> web.Application:
    account = await get_account_async(STORE_PATH, ANISETTE_SERVER, ANISETTE_LIBS_PATH)
    return create_app(account, tracker)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a simple web UI that shows the latest FindMy location."
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--private-key",
        help="Base64 private key for the tag/device",
    )
    source_group.add_argument(
        "--airtag-json",
        type=Path,
        help="Path to an AirTag/accessory JSON file for FindMyAccessory.from_json(...)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
    parser.add_argument(
        "--port",
        type=int,
        default=8008,
        help=(
            "Preferred port to bind (default: 8008). "
            "Use 0 to let the OS pick a free port. "
            "If specified and unavailable, the next free port is used."
        ),
    )
    parser.add_argument(
        "--allow-multiple",
        action="store_true",
        help=(
            "Allow starting a second instance of this app. "
            "By default another copy already running causes exit immediately instead of grabbing another TCP port."
        ),
    )
    args = parser.parse_args()

    if not args.allow_multiple:
        lock_path = _single_instance_lock_path()
        _lock_fd, ok = _acquire_single_instance(lock_path)
        if not ok:
            print(
                f"Another web_tracker_app instance appears to be running (lock: {lock_path}).\n"
                "Stop it first or pass --allow-multiple to run more than one copy.",
                file=sys.stderr,
            )
            sys.exit(1)

    if args.private_key:
        logger.info("Loading tracker from private key...")
        tracker: KeyPair | FindMyAccessory = KeyPair.from_b64(args.private_key)
        logger.info(f"Loaded KeyPair tracker")
    else:
        assert args.airtag_json is not None
        if not args.airtag_json.is_file():
            print(
                f"Error: AirTag JSON file not found: {args.airtag_json}\n"
                "Export/copy your accessory JSON first, then pass its real path with --airtag-json.",
                file=sys.stderr,
            )
            sys.exit(2)
        logger.info(f"Loading tracker from {args.airtag_json}...")
        tracker = FindMyAccessory.from_json(args.airtag_json)
        logger.info(f"Loaded FindMyAccessory: {tracker.name if hasattr(tracker, 'name') else tracker}")

    selected_port = _find_available_port(args.host, args.port)
    if selected_port != args.port:
        logger.warning(f"Port {args.port} is busy, using available port {selected_port} instead.")
    
    logger.info(f"Starting web server at http://{args.host}:{selected_port}")
    logger.info(f"Debug API endpoint available at http://{args.host}:{selected_port}/api/location-history")
    logger.info("Press Ctrl+C to stop the server")
    
    web.run_app(init_app(tracker), host=args.host, port=selected_port)


if __name__ == "__main__":
    main()
