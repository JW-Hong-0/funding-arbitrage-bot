import json
import logging
import os
import time
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

STATE_FILE = "data/bot_state.json"

@dataclass
class DualPosition:
    id: str
    symbol: str              # e.g., "XRP"
    grvt_symbol: str         # e.g., "XRP_USDT_Perp"
    lighter_symbol: str      # e.g., "XRP-USDC"
    entry_time: float
    size: float              # Base asset amount (e.g., 10.0 XRP)
    grvt_entry_price: float
    lighter_entry_price: float
    status: str              # OPENING, HEDGED, CLOSING_GRVT, CLOSING_HEDGE, CLOSED
    pending_hedge_qty: float = 0.0 # Accumulated fills not yet hedged
    funding_collected_count: int = 0
    last_update: float = 0.0
    grvt_side: str = 'long' # 'long' or 'short' (Direction of GRVT entry)

class BotState:
    def __init__(self):
        self.positions: Dict[str, DualPosition] = {}
        self.file_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), STATE_FILE)
        self.load()

    def load(self):
        """Load state from JSON file."""
        if not os.path.exists(self.file_path):
            logger.info("No existing state file found. Starting fresh.")
            return

        try:
            with open(self.file_path, 'r') as f:
                data = json.load(f)
                for pid, pdata in data.get('positions', {}).items():
                    # Check for missing fields in loaded data (migration)
                    if 'pending_hedge_qty' not in pdata:
                        pdata['pending_hedge_qty'] = 0.0
                    if 'grvt_side' not in pdata:
                        pdata['grvt_side'] = 'long' # Default legacy
                    self.positions[pid] = DualPosition(**pdata)
            logger.info(f"Loaded {len(self.positions)} active positions from state.")
        except Exception as e:
            logger.error(f"Failed to load bot state: {e}")

    def save(self):
        """Save state to JSON file."""
        try:
            os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
            data = {
                'positions': {pid: asdict(p) for pid, p in self.positions.items()},
                'updated_at': time.time()
            }
            with open(self.file_path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save bot state: {e}")

    def add_position(self, position: DualPosition):
        self.positions[position.id] = position
        self.save()

    def update_position(self, position: DualPosition):
        position.last_update = time.time()
        self.positions[position.id] = position
        self.save()

    def remove_position(self, position_id: str):
        if position_id in self.positions:
            del self.positions[position_id]
            self.save()

    def get_active_positions(self) -> List[DualPosition]:
        return [p for p in self.positions.values() if p.status != 'CLOSED']

    def get_position_by_symbol(self, symbol: str) -> Optional[DualPosition]:
        # Symbol could be base symbol 'XRP'
        return next((p for p in self.positions.values() if p.symbol == symbol and p.status != 'CLOSED'), None)
