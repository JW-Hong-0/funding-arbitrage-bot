import csv
import logging
import os
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger("SessionLogManager")


class SessionLogManager:
    def __init__(self, base_dir: Optional[str] = None):
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        if base_dir:
            self.base_dir = os.path.join(base_dir, f"session_{timestamp}")
        else:
            self.base_dir = os.path.join(os.getcwd(), "logs", f"session_{timestamp}")
        os.makedirs(self.base_dir, exist_ok=True)

        self.paths = {
            "trades": os.path.join(self.base_dir, "trades.csv"),
            "positions": os.path.join(self.base_dir, "positions.csv"),
            "funding": os.path.join(self.base_dir, "funding.csv"),
            "balances": os.path.join(self.base_dir, "balances.csv"),
            "signals": os.path.join(self.base_dir, "signals.csv"),
        }
        self.headers = {
            "trades": [
                "ts_utc",
                "exchange",
                "symbol",
                "side",
                "order_type",
                "price",
                "qty",
                "notional",
                "fee",
                "fee_asset",
                "order_id",
                "client_order_id",
                "status",
                "reason",
                "strategy_id",
                "signal_id",
            ],
            "positions": [
                "ts_utc",
                "exchange",
                "symbol",
                "side",
                "size",
                "entry_price",
                "mark_price",
                "pnl",
                "leverage",
                "margin_type",
                "liquidation_price",
            ],
            "funding": [
                "ts_utc",
                "exchange",
                "symbol",
                "funding_rate",
                "funding_time",
                "next_funding_time",
            ],
            "balances": [
                "ts_utc",
                "exchange",
                "asset",
                "total",
                "available",
                "margin_used",
            ],
            "signals": [
                "ts_utc",
                "symbol",
                "leg_long",
                "leg_short",
                "spread",
                "projected_yield",
                "decision",
                "reason",
            ],
        }

    @staticmethod
    def _utc_now() -> str:
        return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    def _append_row(self, key: str, row: Dict) -> None:
        path = self.paths.get(key)
        headers = self.headers.get(key)
        if not path or not headers:
            return
        file_exists = os.path.exists(path)
        try:
            with open(path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                if not file_exists:
                    writer.writeheader()
                writer.writerow({h: row.get(h, "") for h in headers})
        except Exception as e:
            logger.warning(f"Failed to append {key} log: {e}")

    def log_trade(self, row: Dict) -> None:
        row = dict(row)
        row.setdefault("ts_utc", self._utc_now())
        self._append_row("trades", row)

    def log_position(self, row: Dict) -> None:
        row = dict(row)
        row.setdefault("ts_utc", self._utc_now())
        self._append_row("positions", row)

    def log_funding(self, row: Dict) -> None:
        row = dict(row)
        row.setdefault("ts_utc", self._utc_now())
        self._append_row("funding", row)

    def log_balance(self, row: Dict) -> None:
        row = dict(row)
        row.setdefault("ts_utc", self._utc_now())
        self._append_row("balances", row)

    def log_signal(self, row: Dict) -> None:
        row = dict(row)
        row.setdefault("ts_utc", self._utc_now())
        self._append_row("signals", row)

    def finalize_to_xlsx(self) -> Optional[str]:
        try:
            import pandas as pd
        except Exception:
            logger.warning("pandas is unavailable; XLSX conversion skipped.")
            return None

        output_path = os.path.join(self.base_dir, "session.xlsx")
        try:
            with pd.ExcelWriter(output_path) as writer:
                for key, path in self.paths.items():
                    if not os.path.exists(path):
                        continue
                    try:
                        df = pd.read_csv(path)
                    except Exception as e:
                        logger.warning(f"Failed to read {path}: {e}")
                        continue
                    sheet = key[:31]
                    df.to_excel(writer, sheet_name=sheet, index=False)
            logger.info(f"XLSX report saved to {output_path}")
            return output_path
        except Exception as e:
            logger.warning(f"XLSX conversion failed: {e}")
            return None
