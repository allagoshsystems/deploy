import asyncio
import json
import os
import time
from datetime import datetime
from typing import Dict, List, Any
from playwright.async_api import async_playwright, Page, BrowserContext, Browser

# JavaScript function to extract odds from Parimatch DOM
JS_EXTRACT_CRICKET_ODDS = """
() => {
    const matches = [];
    
    // Select match cards/containers across live and upcoming feeds
    const matchElements = document.querySelectorAll('[class*="match"], [class*="eventCard"], [data-testid*="match"]');
    
    matchElements.forEach((el, index) => {
        try {
            const textContent = el.innerText || "";
            const lines = textContent.split('\\n').map(s => s.trim()).filter(Boolean);
            
            if (lines.length >= 2) {
                matches.push({
                    id: `match_${index}_${Date.now()}`,
                    raw_title: lines.slice(0, 3).join(' vs '),
                    details: lines,
                    timestamp: new Date().toISOString()
                });
            }
        } catch (err) {
            // Ignore single card parse errors
        }
    });

    return matches;
}
"""

class CricketOddsScraper:
    URL_LIVE = "https://parimatch-asia.net/en/sports/cricket/live"
    URL_UPCOMING = "https://parimatch-asia.net/en/sports/cricket"
    OUTPUT_FILE = "odds.json"

    def __init__(self):
        self.playwright = None
        self.browser: Browser = None
        self.context: BrowserContext = None
        self.page_live: Page = None
        self.page_upcoming: Page = None

    async def start_browser(self):
        """Launches Chromium with realistic user settings to avoid detection."""
        print("  [setup] Launching browser engine...", flush=True)
        self.playwright = await async_playwright().start()
        
        # Launch with headless=False so local browser renders fully
        self.browser = await self.playwright.chromium.launch(
            headless=False,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-blink-features=AutomationControlled'
            ]
        )
        
        self.context = await self.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768}
        )

    async def init_pages(self) -> None:
        """Connects to feeds and waits explicitly for UI elements to hydrate."""
        self.page_live = await self.context.new_page()
        self.page_upcoming = await self.context.new_page()

        print("  [setup] Connecting to Parimatch Live Cricket feed...", flush=True)
        try:
            await self.page_live.goto(self.URL_LIVE, wait_until="domcontentloaded", timeout=60000)
            # Pause to let React/Vue components render matches on screen
            await self.page_live.wait_for_selector('button, [class*="match"], [class*="event"]', timeout=20000)
            await self.page_live.wait_for_timeout(3000)
        except Exception as e:
            print(f"  [setup] Live page load warning: {e}", flush=True)

        print("  [setup] Connecting to Parimatch Upcoming Cricket feed...", flush=True)
        try:
            await self.page_upcoming.goto(self.URL_UPCOMING, wait_until="domcontentloaded", timeout=60000)
            await self.page_upcoming.wait_for_selector('button, [class*="match"], [class*="event"]', timeout=20000)
            await self.page_upcoming.wait_for_timeout(3000)
        except Exception as e:
            print(f"  [setup] Upcoming page load warning: {e}", flush=True)

    async def scrape_tick(self) -> Dict[str, Any]:
        """Extracts odds data from both pages."""
        live_matches = []
        upcoming_matches = []

        try:
            live_matches = await self.page_live.evaluate(JS_EXTRACT_CRICKET_ODDS)
        except Exception as e:
            print(f"Error scraping live feed: {e}", flush=True)

        try:
            upcoming_matches = await self.page_upcoming.evaluate(JS_EXTRACT_CRICKET_ODDS)
        except Exception as e:
            print(f"Error scraping upcoming feed: {e}", flush=True)

        all_matches = live_matches + upcoming_matches

        # Debug check: Save screenshot if 0 matches found
        if len(all_matches) == 0:
            try:
                await self.page_live.screenshot(path="local_debug.png")
            except Exception:
                pass

        payload = {
            "updated_at": datetime.utcnow().isoformat() + "Z",
            "total_matches": len(all_matches),
            "live_count": len(live_matches),
            "upcoming_count": len(upcoming_matches),
            "matches": all_matches
        }

        # Save result atomically to odds.json
        temp_file = f"{self.OUTPUT_FILE}.tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(temp_file, self.OUTPUT_FILE)

        return payload

    async def run_loop(self):
        """Runs continuous scraping loop every 5 seconds."""
        await self.start_browser()
        await self.init_pages()

        print("Feeds active. Starting 5-second tick loop...", flush=True)
        tick = 1

        try:
            while True:
                start_time = time.time()
                data = await self.scrape_tick()
                elapsed_ms = (time.time() - start_time) * 1000

                now_str = datetime.now().strftime("%H:%M:%S")
                print(
                    f"[{now_str}] Tick #{tick:04d} | "
                    f"Live={data['live_count']}  Upcoming={data['upcoming_count']}  "
                    f"Total={data['total_matches']} | {elapsed_ms:.1f}ms -> {self.OUTPUT_FILE}",
                    flush=True
                )

                tick += 1
                await asyncio.sleep(5.0)
        except KeyboardInterrupt:
            print("\nStopping scraper...", flush=True)
        finally:
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()

if __name__ == "__main__":
    scraper = CricketOddsScraper()
    asyncio.run(scraper.run_loop())
