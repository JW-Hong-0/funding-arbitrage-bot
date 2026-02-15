import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from aiohttp import web


class UiServer:
    def __init__(
        self,
        monitor,
        asset_manager,
        trading,
        host: str,
        port: int,
        refresh_s: float = 2.0,
        show_raw: bool = False,
    ):
        self.monitor = monitor
        self.asset_manager = asset_manager
        self.trading = trading
        self.host = host
        self.port = port
        self.refresh_s = refresh_s
        self.show_raw = show_raw
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

    async def start(self) -> None:
        app = web.Application()
        app["ui_server"] = self
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/api/status", self._handle_status)
        app.router.add_post("/api/control/start", self._handle_start)
        app.router.add_post("/api/control/stop", self._handle_stop)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
        self._runner = None
        self._site = None

    async def _handle_index(self, request: web.Request) -> web.Response:
        refresh_ms = max(int(self.refresh_s * 1000), 500)
        raw_section = ""
        if self.show_raw:
            raw_section = """
    <h3>Raw Snapshot</h3>
    <pre id="raw-json"></pre>
"""
        html = f"""
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Funding Arbitrage Bot</title>
    <style>
      body {{ font-family: Arial, sans-serif; margin: 20px; }}
      table {{ border-collapse: collapse; width: 100%; margin-top: 8px; }}
      th, td {{ border: 1px solid #ddd; padding: 6px; text-align: left; }}
      th {{ background: #f4f4f4; }}
      .row {{ display: flex; gap: 12px; flex-wrap: wrap; }}
      .card {{ border: 1px solid #ddd; padding: 12px; border-radius: 6px; min-width: 260px; }}
      .muted {{ color: #666; font-size: 0.9em; }}
      button {{ margin-right: 8px; }}
      pre {{ background: #f9f9f9; padding: 10px; overflow: auto; }}
    </style>
  </head>
  <body>
    <h2>Funding Arbitrage Bot</h2>
    <div class="row">
      <div class="card">
        <div><strong>Status</strong></div>
        <div id="status-line" class="muted">Loading...</div>
        <div style="margin-top: 8px;">
          <button onclick="control('start')">Start Trading</button>
          <button onclick="control('stop')">Stop Trading</button>
        </div>
      </div>
      <div class="card">
        <div><strong>Session PnL</strong></div>
        <div id="pnl-line" class="muted">-</div>
      </div>
    </div>

    <h3>Balances</h3>
    <table id="balances-table">
      <thead><tr><th>Exchange</th><th>Total</th><th>Available</th></tr></thead>
      <tbody></tbody>
    </table>

    <h3>Open Positions</h3>
    <table id="positions-table">
      <thead><tr><th>Ticker</th><th>State</th><th>GRVT</th><th>HYNA</th><th>VAR</th></tr></thead>
      <tbody></tbody>
    </table>

    <h3>Signals</h3>
    <div class="muted">Click column headers to sort (toggle asc/desc).</div>
    <table id="signals-table">
      <thead>
        <tr>
          <th data-sort="ticker">Ticker</th>
          <th data-sort="long">Long</th>
          <th data-sort="short">Short</th>
          <th data-sort="yield">Yield</th>
          <th data-sort="spread">Spread</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>

    <h3>Funding Rates</h3>
    <div class="muted">Click column headers to sort (toggle asc/desc).</div>
    <table id="funding-table">
      <thead>
        <tr>
          <th data-sort="ticker">Ticker</th>
          <th data-sort="GRVT">GRVT Funding (h)</th>
          <th data-sort="GRVT_APR">GRVT APR</th>
          <th data-sort="VAR">VAR Funding (h)</th>
          <th data-sort="VAR_APR">VAR APR</th>
          <th data-sort="HYNA">HYNA Funding (h)</th>
          <th data-sort="HYNA_APR">HYNA APR</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>

{raw_section}

    <script>
      async function control(action) {{
        await fetch(`/api/control/${{action}}`, {{ method: 'POST' }});
      }}
      function formatNum(v) {{
        if (v === null || v === undefined) return '-';
        return Number(v).toFixed(4);
      }}
      function formatPct(v) {{
        if (v === null || v === undefined) return '-';
        return (Number(v) * 100).toFixed(4) + '%';
      }}
      function formatInterval(v) {{
        if (!v) return '-';
        return (Number(v) / 3600).toFixed(2);
      }}
      function formatVenue(venue, ticker, fundingMap) {{
        if (!venue) return '-';
        const info = (fundingMap[ticker] || {{}})[venue] || null;
        const interval = info ? (info.funding_interval_s ?? info.interval_s) : null;
        if (!info || !interval) return venue;
        return `${{venue}} (${{formatInterval(interval)}}h)`;
      }}
      function calcApr(info) {{
        if (!info) return null;
        const interval = Number(info.funding_interval_s ?? info.interval_s ?? 0);
        if (!interval) return null;
        const rate = Number(info.funding_rate || 0);
        if (!interval) return null;
        return rate * (365 * 24 * 3600) / interval;
      }}
      function formatApr(info) {{
        const apr = calcApr(info);
        if (apr === null || apr === undefined || Number.isNaN(apr)) return '-';
        return (apr * 100).toFixed(2) + '%';
      }}
      function normNum(v) {{
        if (v === null || v === undefined || Number.isNaN(v)) return -Infinity;
        return Number(v);
      }}
      let signalsSort = {{ key: 'ticker', dir: 'asc' }};
      let fundingSort = {{ key: 'ticker', dir: 'asc' }};
      function setSignalsSort(key) {{
        if (signalsSort.key === key) {{
          signalsSort.dir = signalsSort.dir === 'asc' ? 'desc' : 'asc';
        }} else {{
          signalsSort = {{ key, dir: 'asc' }};
        }}
      }}
      function setFundingSort(key) {{
        if (fundingSort.key === key) {{
          fundingSort.dir = fundingSort.dir === 'asc' ? 'desc' : 'asc';
        }} else {{
          fundingSort = {{ key, dir: 'asc' }};
        }}
      }}
      async function refresh() {{
        const resp = await fetch('/api/status');
        const data = await resp.json();
        const tradingState = data.paused ? 'Trading: Paused' : 'Trading: Running';
        document.getElementById('status-line').innerText = `Monitoring: Running | ${{tradingState}}`;
        document.getElementById('pnl-line').innerText = formatNum(data.session_pnl);

        const bbody = document.querySelector('#balances-table tbody');
        bbody.innerHTML = '';
        for (const [ex, vals] of Object.entries(data.balances || {{}})) {{
          const row = document.createElement('tr');
          row.innerHTML = `<td>${{ex}}</td><td>${{formatNum(vals.total)}}</td><td>${{formatNum(vals.available)}}</td>`;
          bbody.appendChild(row);
        }}

        const pbody = document.querySelector('#positions-table tbody');
        pbody.innerHTML = '';
        for (const [ticker, pos] of Object.entries(data.positions || {{}})) {{
          if (pos.State === 'IDLE') continue;
          const row = document.createElement('tr');
          row.innerHTML = `<td>${{ticker}}</td><td>${{pos.State}}</td><td>${{formatNum(pos.GRVT)}}</td><td>${{formatNum(pos.HYNA)}}</td><td>${{formatNum(pos.VAR)}}</td>`;
          pbody.appendChild(row);
        }}

        const sbody = document.querySelector('#signals-table tbody');
        sbody.innerHTML = '';
        const fundingMap = data.funding_map || {{}};
        const signalsRows = Object.entries(data.signals || {{}}).map(([ticker, sig]) => {{
          return {{ ticker, sig }};
        }});
        signalsRows.sort((a, b) => {{
          let av = '';
          let bv = '';
          if (signalsSort.key === 'ticker') {{
            av = a.ticker || '';
            bv = b.ticker || '';
          }} else if (signalsSort.key === 'long') {{
            av = a.sig.long || '';
            bv = b.sig.long || '';
          }} else if (signalsSort.key === 'short') {{
            av = a.sig.short || '';
            bv = b.sig.short || '';
          }} else if (signalsSort.key === 'yield') {{
            av = normNum(a.sig.projected_yield || 0);
            bv = normNum(b.sig.projected_yield || 0);
          }} else if (signalsSort.key === 'spread') {{
            av = normNum(a.sig.spread || 0);
            bv = normNum(b.sig.spread || 0);
          }}
          if (typeof av === 'string') {{
            return signalsSort.dir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av);
          }}
          return signalsSort.dir === 'asc' ? av - bv : bv - av;
        }});
        for (const item of signalsRows) {{
          const ticker = item.ticker;
          const sig = item.sig;
          const row = document.createElement('tr');
          const longLabel = formatVenue(sig.long, ticker, fundingMap);
          const shortLabel = formatVenue(sig.short, ticker, fundingMap);
          row.innerHTML = `<td>${{ticker}}</td><td>${{longLabel}}</td><td>${{shortLabel}}</td><td>${{formatNum(sig.projected_yield * 100)}}</td><td>${{formatNum(sig.spread * 100)}}</td>`;
          sbody.appendChild(row);
        }}

        const fbody = document.querySelector('#funding-table tbody');
        fbody.innerHTML = '';
        const rows = Array.from(data.funding_table || []);
        rows.sort((a, b) => {{
          let av = '';
          let bv = '';
          if (fundingSort.key === 'ticker') {{
            av = a.ticker || '';
            bv = b.ticker || '';
          }} else if (fundingSort.key.endsWith('_APR')) {{
            const venue = fundingSort.key.replace('_APR', '');
            av = calcApr((a[venue] || {{}}));
            bv = calcApr((b[venue] || {{}}));
          }} else {{
            av = (a[fundingSort.key] || {{}}).funding_rate;
            bv = (b[fundingSort.key] || {{}}).funding_rate;
          }}
          if (typeof av === 'string') {{
            return fundingSort.dir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av);
          }}
          av = normNum(av);
          bv = normNum(bv);
          return fundingSort.dir === 'asc' ? av - bv : bv - av;
        }});
        for (const rowData of rows) {{
          const row = document.createElement('tr');
          function cellFor(ex) {{
            const info = rowData[ex] || {{}};
            if (!info || info.funding_rate === undefined || info.funding_rate === null) return '-';
            return `${{formatPct(info.funding_rate)}} (${{formatInterval(info.funding_interval_s)}}h)`;
          }}
          row.innerHTML = `<td>${{rowData.ticker}}</td><td>${{cellFor('GRVT')}}</td><td>${{formatApr(rowData.GRVT)}}</td><td>${{cellFor('VAR')}}</td><td>${{formatApr(rowData.VAR)}}</td><td>${{cellFor('HYNA')}}</td><td>${{formatApr(rowData.HYNA)}}</td>`;
          fbody.appendChild(row);
        }}

        if ({str(self.show_raw).lower()}) {{
          const rawEl = document.getElementById('raw-json');
          if (rawEl) {{
            rawEl.innerText = JSON.stringify(data, null, 2);
          }}
        }}
      }}
      refresh();
      setInterval(refresh, {refresh_ms});
      document.querySelectorAll('#signals-table thead th').forEach((th) => {{
        const key = th.getAttribute('data-sort');
        if (!key) return;
        th.style.cursor = 'pointer';
        th.addEventListener('click', () => {{
          setSignalsSort(key);
          refresh();
        }});
      }});
      document.querySelectorAll('#funding-table thead th').forEach((th) => {{
        const key = th.getAttribute('data-sort');
        if (!key) return;
        th.style.cursor = 'pointer';
        th.addEventListener('click', () => {{
          setFundingSort(key);
          refresh();
        }});
      }});
    </script>
  </body>
</html>
"""
        return web.Response(text=html, content_type="text/html")

    async def _handle_status(self, request: web.Request) -> web.Response:
        funding_table = []
        funding_map: Dict[str, Dict[str, Any]] = {}
        for ticker, mdata in (self.monitor.market_data or {}).items():
            row = {"ticker": ticker}
            for ex_name in ("GRVT", "VAR", "HYNA"):
                ex_data = (mdata or {}).get(ex_name)
                if not ex_data or ex_data.get("funding_rate") is None:
                    continue
                row[ex_name] = {
                    "funding_rate": ex_data.get("funding_rate"),
                    "funding_interval_s": ex_data.get("funding_interval_s"),
                    "next_funding_time": ex_data.get("next_funding_time"),
                }
                funding_map.setdefault(ticker, {})[ex_name] = row[ex_name]
            funding_table.append(row)
        data: Dict[str, Any] = {
            "timestamp": time.time(),
            "time_utc": datetime.now(timezone.utc).isoformat(),
            "paused": bool(getattr(self.trading, "paused", False)),
            "balances": self.asset_manager.balances,
            "total_balance": self.asset_manager.total_balance(),
            "session_pnl": self.asset_manager.session_pnl(),
            "positions": self.asset_manager.positions,
            "signals": self._serialize_signals(),
            "funding_table": funding_table,
            "funding_map": funding_map,
        }
        return web.json_response(data)

    async def _handle_start(self, request: web.Request) -> web.Response:
        if hasattr(self.trading, "set_paused"):
            self.trading.set_paused(False, reason="ui")
        return web.json_response({"ok": True, "paused": False})

    async def _handle_stop(self, request: web.Request) -> web.Response:
        if hasattr(self.trading, "set_paused"):
            self.trading.set_paused(True, reason="ui")
        return web.json_response({"ok": True, "paused": True})

    def _serialize_signals(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for ticker, sig in (self.monitor.signals or {}).items():
            out[ticker] = {
                "long": getattr(sig, "best_ask_venue", ""),
                "short": getattr(sig, "best_bid_venue", ""),
                "spread": float(getattr(sig, "spread", 0.0) or 0.0),
                "projected_yield": float(getattr(sig, "projected_yield", 0.0) or 0.0),
                "is_opportunity": bool(getattr(sig, "is_opportunity", False)),
            }
        return out
