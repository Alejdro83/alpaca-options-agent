"""Offline test for shadow_book — inserts fake test rows against the REAL
database (cycle_id = -999 so they're obviously not real cycles). Verifies
both vertical and iron_condor candidates flow through open_counterfactuals
with the correct policy/strategy/same_as_llm values.

Does NOT make any real MCP/Alpaca calls — all candidates and plans are
constructed manually.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from dataclasses import dataclass

# Load .env
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    for line in open(_env_path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip("'\"")
            os.environ.setdefault(key, val)

# Minimal stubs for the plan dataclasses (can't import spread_builder without
# MCP dependencies, so we replicate the fields shadow_book actually reads).
@dataclass
class FakeSpreadPlan:
    underlying: str
    direction: str
    expiration: date
    short_strike: float
    long_strike: float
    short_symbol: str
    long_symbol: str
    credit_estimate: float
    max_loss: float

@dataclass
class FakeIronCondorPlan:
    underlying: str
    direction: str
    expiration: date
    short_put_strike: float
    long_put_strike: float
    short_call_strike: float
    long_call_strike: float
    short_put_symbol: str
    long_put_symbol: str
    short_call_symbol: str
    long_call_symbol: str
    credit_estimate: float
    max_loss: float

import shadow_book

CYCLE_ID = -999
EQUITY = 100_000.0
MAX_RISK_PCT = 0.02

# Build fake candidates: one vertical, one iron condor
today = date.today()
exp = today + timedelta(days=10)

vertical_plan = FakeSpreadPlan(
    underlying="AAPL",
    direction="bull_put",
    expiration=exp,
    short_strike=200.0,
    long_strike=195.0,
    short_symbol="AAPL250912P00200000",
    long_symbol="AAPL250912P00195000",
    credit_estimate=150.0,
    max_loss=350.0,
)

ic_plan = FakeIronCondorPlan(
    underlying="MSFT",
    direction="iron_condor",
    expiration=exp,
    short_put_strike=420.0,
    long_put_strike=415.0,
    short_call_strike=460.0,
    long_call_strike=465.0,
    short_put_symbol="MSFT250912P00420000",
    long_put_symbol="MSFT250912P00415000",
    short_call_symbol="MSFT250912C00460000",
    long_call_symbol="MSFT250912C00465000",
    credit_estimate=200.0,
    max_loss=300.0,
)

candidates = [
    {"ticker": "AAPL", "strategy": "vertical", "direction": "long", "strength": 0.8, "credit_estimate": 150.0, "max_loss": 350.0, "_plan": vertical_plan},
    {"ticker": "MSFT", "strategy": "iron_condor", "direction": None, "strength": None, "credit_estimate": 200.0, "max_loss": 300.0, "_plan": ic_plan},
]

# LLM picked AAPL only; shadow rule picks both; random picks from the menu
llm_selected = ["AAPL"]
shadow_selected = ["AAPL", "MSFT"]

print(f"Cycle ID: {CYCLE_ID}")
print(f"LLM selected: {llm_selected}")
print(f"Shadow selected: {shadow_selected}")
print(f"Equity: ${EQUITY:,.2f}")
print()

shadow_book.open_counterfactuals(
    cycle_id=CYCLE_ID,
    candidates=candidates,
    llm_selected=llm_selected,
    shadow_selected=shadow_selected,
    equity=EQUITY,
    max_risk_pct=MAX_RISK_PCT,
)

print("open_counterfactuals returned — querying inserted rows...\n")

import psycopg2
import psycopg2.extras
from config import config

conn = psycopg2.connect(
    host=config.supabase.db_host,
    port=config.supabase.db_port,
    dbname=config.supabase.db_name,
    user=config.supabase.db_user,
    password=config.supabase.db_password,
    sslmode="require",
)
with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
    cur.execute(
        "select * from alpaca_hackathon.shadow_positions where cycle_id = %s order by policy, id",
        (CYCLE_ID,),
    )
    rows = cur.fetchall()

print(f"Found {len(rows)} rows for cycle_id={CYCLE_ID}:\n")
for r in rows:
    print(f"  policy={r['policy']:8s}  strategy={r['strategy']:13s}  underlying={r['underlying']:5s}  "
          f"contracts={r['contracts']}  credit={r['credit_received']}  max_loss={r['max_loss']}  "
          f"same_as_llm={r['same_as_llm']}  status={r['status']}")

# Verify expectations
print("\n--- Verification ---")
policies = {r['policy'] for r in rows}
assert 'shadow' in policies, "Expected 'shadow' policy rows"
assert 'random' in policies, "Expected 'random' policy rows"
print(f"[OK] Both policies present: {policies}")

shadow_rows = [r for r in rows if r['policy'] == 'shadow']
random_rows = [r for r in rows if r['policy'] == 'random']

# Shadow policy should have picked AAPL (vertical) and MSFT (iron condor)
shadow_underlyings = {r['underlying'] for r in shadow_rows}
assert shadow_underlyings == {"AAPL", "MSFT"}, f"Expected AAPL+MSFT for shadow, got {shadow_underlyings}"
print(f"[OK] Shadow policy underlyings: {shadow_underlyings}")

# Random policy should pick 1 candidate (matched to LLM's trade count)
assert len(random_rows) == len(llm_selected), f"Expected {len(llm_selected)} random rows, got {len(random_rows)}"
print(f"[OK] Random policy picked {len(random_rows)} (matched LLM's {len(llm_selected)})")

# Check strategies
for r in rows:
    if r['underlying'] == 'AAPL':
        assert r['strategy'] == 'vertical', f"AAPL should be vertical, got {r['strategy']}"
    elif r['underlying'] == 'MSFT':
        assert r['strategy'] == 'iron_condor', f"MSFT should be iron_condor, got {r['strategy']}"

# Check same_as_llm: AAPL should be True for shadow (LLM also picked AAPL)
aapl_shadow = [r for r in shadow_rows if r['underlying'] == 'AAPL']
assert len(aapl_shadow) == 1
assert aapl_shadow[0]['same_as_llm'] == True, "AAPL shadow should be same_as_llm=True"
msft_shadow = [r for r in shadow_rows if r['underlying'] == 'MSFT']
assert len(msft_shadow) == 1
assert msft_shadow[0]['same_as_llm'] == False, "MSFT shadow should be same_as_llm=False"
print("[OK] same_as_llm values correct")

# Check contract sizing: equity=100k, max_risk_pct=0.02 -> budget=2000
# AAPL: max_loss=350 -> contracts = int(2000//350) = 5
# MSFT: max_loss=300 -> contracts = int(2000//300) = 6
for r in rows:
    expected = int((EQUITY * MAX_RISK_PCT) // float(r['max_loss']))
    assert r['contracts'] == expected, f"{r['underlying']} contracts={r['contracts']}, expected={expected}"
print("[OK] Contract sizing correct")

# Check iron condor fields for MSFT
msft_all = [r for r in rows if r['underlying'] == 'MSFT']
for r in msft_all:
    assert r['call_short_strike'] == 460.0, f"call_short_strike={r['call_short_strike']}"
    assert r['call_long_strike'] == 465.0, f"call_long_strike={r['call_long_strike']}"
    assert r['call_short_symbol'] is not None
    assert r['call_long_symbol'] is not None
    assert r['direction'] is None  # iron condor has no directional signal
print("[OK] Iron condor fields populated correctly")

print("\nAll verifications passed!")
conn.close()
