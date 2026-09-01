"""
Cricket Odds Live Scraper
Streams REAL cricket match odds (Live in-play + Upcoming fixtures) from Parimatch
with NO LIMIT and updates odds.json every 5 seconds.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

# ---------------------------------------------------------------------------
# JavaScript snippet for extracting ALL cricket matches & odds from DOM
# ---------------------------------------------------------------------------
JS_EXTRACT_CRICKET_ODDS = """
(() => {
    const results = [];
    const seenMatches = new Set();

    // Find all buttons with decimal odds (e.g. 1.01 to 99.99)
    const allOddsButtons = Array.from(document.querySelectorAll('button')).filter(b => {
        const t = (b.innerText || '').trim();
        return /\\b[1-9]\\d*\\.\\d{2}\\b/.test(t);
    });

    // Identify candidate match card / row containers
    const candidateContainers = new Set();
    allOddsButtons.forEach(btn => {
        let cur = btn;
        for (let i = 0; i < 6; i++) {
            if (!cur.parentElement) break;
            cur = cur.parentElement;
            const text = cur.innerText || '';
            const lines = text.split('\\n').map(l => l.trim()).filter(Boolean);
            const btnCount = cur.querySelectorAll('button').length;
            if (lines.length >= 3 && lines.length <= 35 && btnCount >= 2 && btnCount <= 16) {
                candidateContainers.add(cur);
                break;
            }
        }
    });

    // Also include containers around cricket links
    document.querySelectorAll('a[href*="/cricket/"], a[href*="/events/"]').forEach(a => {
        let cur = a;
        for (let i = 0; i < 4; i++) {
            if (!cur.parentElement) break;
            cur = cur.parentElement;
            if (cur.querySelectorAll('button').length >= 2) {
                candidateContainers.add(cur);
                break;
            }
        }
    });

    const FORBIDDEN_WORDS = new Set([
        'lobby', 'prematch', 'live', 'sport', 'sports', 'casino', 'live casino', 
        'esports', 'promotions', 'menu', 'deposit', 'cashier', 'home', 'cricket', 
        'profile', 'betslip', 'my bets', 'search', 'settings', 'all', 'top', 
        'parimatch', 'featured', 'highlights', 'in-play', 'upcoming', 'results',
        'favorites', 'favourites', 'football', 'soccer', 'basketball', 'tennis',
        'table tennis', 'volleyball', 'ice hockey', 'ufc', 'mma', 'baseball',
        'boxing', 'handball', 'futsal', 'badminton', 'darts', 'snooker', 'rugby',
        'horse racing', 'kabaddi', 'virtual sports', 'instant games', 'tv games',
        'field hockey', 'biathlon', 'water polo', 'formula 1', 'motorsport',
        'cycling', 'golf', 'squash', 'floorball', 'bandy', 'aussie rules',
        'gaelic football', 'hurling', 'lacrosse', 'netball', 'pesapallo',
        'speedway', 'surfing', 'swimming', 'athletics', 'alpine skiing',
        'cross-country', 'ski jumping', 'bobsleigh', 'curling', 'figure skating'
    ]);

    // Helper to find tournament title
    function getTournamentTitle(el) {
        let cur = el;
        // 1. Search up the tree
        for (let i = 0; i < 10; i++) {
            if (!cur.parentElement) break;
            cur = cur.parentElement;
            
            // Look for tournament header elements inside or preceding this container
            const headerEl = cur.querySelector('h1, h2, h3, [class*="Header"], [class*="header"], [class*="category"], [class*="tournament"]');
            if (headerEl && headerEl.innerText) {
                const hText = headerEl.innerText.split('\\n')[0].trim();
                if (hText.length > 3 && hText.length < 70 && !FORBIDDEN_WORDS.has(hText.toLowerCase())) {
                    return hText;
                }
            }

            const text = cur.innerText || '';
            const firstLine = text.split('\\n').map(l => l.trim()).filter(Boolean)[0] || '';
            if (firstLine && firstLine.length > 3 && firstLine.length < 70 && (
                firstLine.includes('.') || 
                firstLine.includes('League') || 
                firstLine.includes('Cup') || 
                firstLine.includes('T20') || 
                firstLine.includes('Test') || 
                firstLine.includes('ODI') || 
                firstLine.includes('Premier') ||
                firstLine.includes('Trophy') ||
                firstLine.includes('Series') ||
                firstLine.includes('teams') ||
                firstLine.includes('Clubs') ||
                firstLine.includes('International') ||
                firstLine.includes('Australia') ||
                firstLine.includes('West Indies') ||
                firstLine.includes('India') ||
                firstLine.includes('England')
            )) {
                return firstLine;
            }
        }
        return "Cricket";
    }

    candidateContainers.forEach(container => {
        // Skip navigation, header, sidebar, or footer containers
        if (container.closest('nav, aside, header, footer, [class*="sidebar"], [class*="nav"], [class*="menu"], [class*="header"]')) {
            return;
        }
        const text = container.innerText || '';
        const lines = text.split('\\n').map(l => l.trim()).filter(Boolean);
        if (lines.length < 3) return;

        const linkEl = container.querySelector('a[href*="/cricket/"]') || 
                       container.querySelector('a[href*="/events/"]') || 
                       container.querySelector('a');
        const href = linkEl ? linkEl.href : '';

        // Extract all odds buttons inside this specific match container
        const btns = Array.from(container.querySelectorAll('button'));
        const oddsList = [];
        btns.forEach(b => {
            const bTxt = (b.innerText || '').trim();
            const m = bTxt.match(/\\b([1-9]\\d*\\.\\d{2})\\b/);
            if (m) {
                const oddVal = parseFloat(m[1]);
                if (oddVal >= 1.01 && oddVal <= 100.0) {
                    const label = bTxt.replace(m[0], '').replace(/\\n+/g, ' ').trim();
                    oddsList.push({ odd: oddVal, label: label, raw: bTxt.replace(/\\n+/g, ' ') });
                }
            }
        });

        if (oddsList.length < 2) return;

        let stage = "upcoming";
        let stateInfo = "";
        let team1 = "";
        let team2 = "";
        let score1 = "";
        let score2 = "";

        const teamCandidates = [];
        for (let i = 0; i < lines.length; i++) {
            const line = lines[i];
            const upper = line.toUpperCase();
            if (upper.includes("LIVE") || upper.includes("BREAK") || upper.includes("INNINGS") || upper.includes("SET") || upper.includes("STUMPS")) {
                stage = "live";
                stateInfo = line;
                continue;
            }
            if (upper.includes("TODAY") || upper.includes("TOMORROW") || /^\\d{1,2}:\\d{2}$/.test(line)) {
                stage = "upcoming";
                stateInfo = line;
                continue;
            }
            if (/^\\d+\\/\\d+/.test(line) || /^\\d+\\(\\d+/.test(line)) {
                if (!score1) score1 = line;
                else if (!score2) score2 = line;
                continue;
            }
            if (/^\\d+(\\.\\d+)?$/.test(line)) continue;
            if (/^(OVER|UNDER|1|2|X|Toss|Total|Innings|Over #)/i.test(line)) continue;
            // Skip lines that look like tournaments, dates, headers or non-team phrases
            if (line.includes('.') || 
                line.includes('League') || 
                line.includes('Cup') || 
                line.includes('Test') || 
                line.includes('Trophy') || 
                line.includes('Series') || 
                line.includes('Premier') || 
                line.includes('Championship') || 
                line.includes('teams') || 
                line.includes('Clubs') || 
                line.includes('National')) {
                continue;
            }

            const lLower = line.toLowerCase();
            let hasForbidden = false;
            FORBIDDEN_WORDS.forEach(w => {
                if (lLower === w || lLower.startsWith(w + ' ') || lLower.endsWith(' ' + w)) {
                    hasForbidden = true;
                }
            });
            if (hasForbidden) continue;

            if (line.length >= 2 && line.length <= 50) {
                teamCandidates.push(line);
            }
        }

        if (teamCandidates.length >= 2) {
            team1 = teamCandidates[0];
            team2 = teamCandidates[1];
        } else if (href) {
            const slugMatch = href.match(/cricket\\/[^/]+\\/([^/]+)/);
            if (slugMatch) {
                const parts = slugMatch[1].split('-');
                const half = Math.floor(parts.length / 2);
                team1 = parts.slice(0, half).join(' ');
                team2 = parts.slice(half).join(' ');
            }
        }

        if (!team1 || !team2) return;
        // Strip any leading numbers or indices
        team1 = team1.replace(/^(1|2)\\s+/, '').trim();
        team2 = team2.replace(/^(1|2)\\s+/, '').trim();

        if (team1.toLowerCase() === team2.toLowerCase()) return;
        if (FORBIDDEN_WORDS.has(team1.toLowerCase()) || FORBIDDEN_WORDS.has(team2.toLowerCase())) return;

        const matchKey = `${team1} vs ${team2}`;
        if (seenMatches.has(matchKey)) return;
        seenMatches.add(matchKey);

        const tournament = getTournamentTitle(container);

        // Parse structured odds
        let team1_odds = null;
        let team2_odds = null;
        let draw_odds = null;
        let toss1_odds = null;
        let toss2_odds = null;
        const additionalMarkets = [];

        for (let k = 0; k < oddsList.length; k++) {
            const o = oddsList[k];
            if (k === 0 && (o.label === '1' || o.label === '' || !o.label)) {
                team1_odds = o.odd;
            } else if (k === 1 && (o.label === '2' || o.label === '' || !o.label)) {
                team2_odds = o.odd;
            } else if (o.label.includes('OVER') || o.label.includes('UNDER')) {
                additionalMarkets.push({ selection: o.label || o.raw, odds: o.odd });
            } else if (k === 2 && oddsList.length >= 4 && (o.label === '1' || o.label === '')) {
                toss1_odds = o.odd;
            } else if (k === 3 && oddsList.length >= 4 && (o.label === '2' || o.label === '')) {
                toss2_odds = o.odd;
            }
        }

        if (team1_odds === null && oddsList.length >= 2) {
            team1_odds = oddsList[0].odd;
            team2_odds = oddsList[1].odd;
        }

        const matchId = href.match(/([a-f0-9]{20,}|\\d{6,})/i) ? 
                        href.match(/([a-f0-9]{20,}|\\d{6,})/i)[1] : 
                        `pm_cricket_${matchKey.toLowerCase().replace(/[^a-z0-9]/g, '_')}`;

        results.push({
            id: matchId,
            sport: "Cricket",
            tournament: tournament,
            country: "International",
            match: matchKey,
            teams: { team1: team1, team2: team2 },
            stage: stage,
            status: stage === "live" ? "in_play" : "scheduled",
            state_info: stateInfo || (stage === "live" ? "LIVE" : "SCHEDULED"),
            score: (score1 || score2) ? { team1_score: score1, team2_score: score2 } : null,
            url: href || "https://parimatch-asia.net/en/sports/cricket",
            odds: {
                match_winner: {
                    team1_odds: team1_odds,
                    team2_odds: team2_odds,
                    draw_odds: draw_odds
                },
                toss_winner: (toss1_odds && toss2_odds) ? { team1_odds: toss1_odds, team2_odds: toss2_odds } : null,
                additional_markets: additionalMarkets
            },
            all_odds_raw: oddsList
        });
    });

    return results;
})()
"""

# ---------------------------------------------------------------------------
# Scraper Engine Class
# ---------------------------------------------------------------------------
class ParimatchCricketLiveScraper:
    """
    High-performance real-time Parimatch Cricket Scraper.
    Streams live and prematch cricket odds every 5 seconds without limits.
    """

    URL_LIVE = "https://parimatch-asia.net/en/sports/cricket/live"
    URL_UPCOMING = "https://parimatch-asia.net/en/sports/cricket"

    def __init__(
        self,
        output_file: str = "odds.json",
        interval: float = 5.0,
        headless: bool = True
    ):
        self.output_file = output_file
        self.interval = interval
        self.headless = headless
        self.running = True
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page_live: Optional[Page] = None
        self.page_upcoming: Optional[Page] = None
        self.all_matches: Dict[str, Dict[str, Any]] = {}

    def atomic_write(self, payload: Dict[str, Any]) -> None:
        """Atomically write JSON to output file to avoid partial reads."""
        tmp = self.output_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        if os.path.exists(self.output_file):
            os.replace(tmp, self.output_file)
        else:
            os.rename(tmp, self.output_file)

    async def init_pages(self) -> None:
        """Initialize browser, context, and persistent pages."""
        print("  [setup] Launching browser engine...", flush=True)
        self.page_live = await self.context.new_page()
        self.page_upcoming = await self.context.new_page()

        print("  [setup] Connecting to Parimatch Live Cricket feed...", flush=True)
        try:
            await self.page_live.goto(self.URL_LIVE, wait_until="domcontentloaded", timeout=30000)
            try:
                await self.page_live.wait_for_selector('button', timeout=8000)
            except Exception:
                pass
            await self.page_live.wait_for_timeout(3000)
        except Exception as e:
            print(f"  [setup] Warning loading live page: {e}", flush=True)

        print("  [setup] Connecting to Parimatch Upcoming Cricket feed...", flush=True)
        try:
            await self.page_upcoming.goto(self.URL_UPCOMING, wait_until="domcontentloaded", timeout=30000)
            try:
                await self.page_upcoming.wait_for_selector('button', timeout=8000)
            except Exception:
                pass
            await self.page_upcoming.wait_for_timeout(3000)
        except Exception as e:
            print(f"  [setup] Warning loading upcoming page: {e}", flush=True)

    async def extract_matches(self, page: Optional[Page], feed_name: str) -> List[Dict[str, Any]]:
        """Extract all cricket matches and odds from a page."""
        if not page or page.is_closed():
            return []
        try:
            data = await page.evaluate(JS_EXTRACT_CRICKET_ODDS)
            return data if isinstance(data, list) else []
        except Exception as e:
            print(f"  [{feed_name}] Extraction error: {e}", flush=True)
            return []

    def build_payload(self, iteration: int, matches: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Construct the complete JSON payload."""
        now = datetime.now(timezone.utc).isoformat()
        live_count = sum(1 for m in matches if m.get("stage") == "live")
        upcoming_count = len(matches) - live_count

        return {
            "source": "Parimatch Cricket Live Feed",
            "sport": "cricket",
            "last_updated": now,
            "iteration": iteration,
            "interval_seconds": self.interval,
            "total_matches": len(matches),
            "live_matches": live_count,
            "upcoming_matches": upcoming_count,
            "matches": matches
        }

    async def run(self, once: bool = False) -> None:
        """Main real-time loop."""
        print("=" * 70)
        print("CRICKET ODDS LIVE SCRAPER | PARIMATCH REAL-TIME FEED")
        print(f"Output: {self.output_file}  |  Interval: {self.interval}s  |  Mode: {'Single-shot' if once else 'Continuous 5s Feed'}")
        print(f"Feeds: {self.URL_LIVE} & {self.URL_UPCOMING}")
        print("=" * 70)

        async with async_playwright() as p:
            self.browser = await p.chromium.launch(
                headless=self.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox"
                ]
            )
            self.context = await self.browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080}
            )

            await self.init_pages()
            print("Feeds active. Starting 5-second tick loop...\n", flush=True)

            iteration = 0
            UPCOMING_REFRESH_INTERVAL = 6  # Re-evaluate upcoming page every ~30s

            while self.running:
                iteration += 1
                t0 = time.time()

                try:
                    # 1. Extract live matches (every 5 seconds)
                    live_matches = await self.extract_matches(self.page_live, "LIVE")

                    # 2. Extract upcoming matches (on 1st iteration or periodically)
                    upcoming_matches = []
                    if iteration == 1 or iteration % UPCOMING_REFRESH_INTERVAL == 0:
                        upcoming_matches = await self.extract_matches(self.page_upcoming, "UPCOMING")
                        for m in upcoming_matches:
                            self.all_matches[m["match"]] = m

                    # 3. Merge live matches (Live always updates / takes priority)
                    for m in live_matches:
                        self.all_matches[m["match"]] = m

                    # Build sorted list of matches (Live first, then Upcoming)
                    sorted_matches = sorted(
                        list(self.all_matches.values()),
                        key=lambda x: (0 if x.get("stage") == "live" else 1, x.get("tournament", ""), x.get("match", ""))
                    )

                    # 4. Write payload atomically
                    payload = self.build_payload(iteration, sorted_matches)
                    self.atomic_write(payload)

                    elapsed = time.time() - t0
                    timestamp = datetime.now().strftime("%H:%M:%S")

                    print(
                        f"[{timestamp}] Tick #{iteration:04d} | "
                        f"Live={payload['live_matches']}  Upcoming={payload['upcoming_matches']}  Total={payload['total_matches']} | "
                        f"{elapsed*1000:.1f}ms -> {self.output_file}",
                        flush=True
                    )

                    # Print quick preview of live matches
                    for m in sorted_matches:
                        if m.get("stage") == "live":
                            mw = m.get("odds", {}).get("match_winner", {})
                            sc = m.get("score")
                            score_str = f" | Score: {sc.get('team1_score','')}" if sc else ""
                            print(
                                f"   [LIVE] {m['match']} ({m.get('tournament','Cricket')}) | "
                                f"Odds: {mw.get('team1_odds')} / {mw.get('team2_odds')}{score_str}",
                                flush=True
                            )

                except Exception as e:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Tick #{iteration} error: {e}", flush=True)
                    import traceback
                    traceback.print_exc()

                if once:
                    break

                # Sleep remainder of interval
                sleep_time = max(0.05, self.interval - (time.time() - t0))
                await asyncio.sleep(sleep_time)

            await self.browser.close()


def main():
    parser = argparse.ArgumentParser(description="Parimatch Cricket Live Odds Scraper")
    parser.add_argument("--output", "-o", default="odds.json", help="Output JSON file (default: odds.json)")
    parser.add_argument("--interval", "-i", type=float, default=5.0, help="Refresh interval in seconds (default: 5.0)")
    parser.add_argument("--visible", action="store_true", default=False, help="Show browser window for visual debugging")
    parser.add_argument("--once", action="store_true", help="Run single cycle and exit")
    args = parser.parse_args()

    scraper = ParimatchCricketLiveScraper(
        output_file=args.output,
        interval=args.interval,
        headless=not args.visible
    )

    try:
        asyncio.run(scraper.run(once=args.once))
    except KeyboardInterrupt:
        print("\nScraper stopped by user.")


if __name__ == "__main__":
    main()
