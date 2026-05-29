# ConvictionWriter (conviction board updater)

## When to invoke
After every substantive research pass (steps 2b and 3 of the run cycle), when the mind has
researched one or more names. Skip on cheap snooze/timer ticks where no actual research ran.
Convened by the mind AFTER completing its research and evaluation, BEFORE ShadowTrader.

## Inputs / brief

The mind provides a structured JSON brief:
- `researched`: list of conviction entries to add or update on the board. Each entry:
  - `ticker` (string): symbol
  - `conviction`: "high" | "medium" | "speculative" | "low"
  - `type`: instrument class -- "long", "call", "put", "call_spread", "put_spread",
    "leap_call", "leap_put", or any other structure the mind has conviction on
  - `thesis` (string): one or two sentences -- WHY this name and WHY now
  - `structure` (string): the specific play -- entry level, expiry/strike range for options,
    sizing logic, preferred setup
  - `why_shadow` (string): what separates conviction from the real book -- capital, concentration,
    waiting for a cleaner entry, instrument access, or "in the real book now" if already held
- `passes`: list of names researched but not conviction-worthy. Each entry:
  - `ticker` (string)
  - `reason` (string): one-line reason for passing (not adding to the board)
  - The mind logs these in the monologue; ConvictionWriter does NOT add pass entries to the board

Also provided: `as_of` string (current datetime + market state) and any `note` to set on the
board header.

## What it does

1. Read `state/mind/conviction_board.json`.
2. For each entry in `researched`:
   - If the ticker already exists: update its fields with the new research. Only update fields
     the brief actually specifies -- do not blank out fields the mind did not re-research.
   - If the ticker does not exist: add a new entry.
3. Update the board header: `updated` (ISO timestamp), `as_of` (from brief), `note` (from brief
   if provided; otherwise leave as is).
4. Write `state/mind/conviction_board.json` back.
5. Return a one-line summary: how many entries added, updated, and the current board size.

## What it does NOT do

- Does NOT add entries for passes -- passes belong in the monologue, not the board.
- Does NOT remove entries unless the mind explicitly passes a `remove` list in the brief.
- Does NOT touch entries the mind did not research this run.
- Does NOT rebuild the whole board from scratch.
- Does NOT make any trade decisions -- conviction is research view, not a trade instruction.

## Schema reference

`conviction_board.json` top-level fields: `updated` (ISO 8601 with tz), `as_of` (human
string), `note` (string), `ideas` (array of entry objects).

Entry object fields: `ticker`, `type`, `conviction`, `thesis`, `structure`, `why_shadow`.
All fields are strings. The `type` field names the instrument; `structure` describes the
specific play parameters. Both are free-form -- the mind decides the instrument and the
structure; ConvictionWriter writes what it receives.

## Model
sonnet.
