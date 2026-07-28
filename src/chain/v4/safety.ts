/** Pure, side-effect-free bounds used by v4 mint/close paths. */
export function operationAmount(after: bigint, before: bigint, preexistingCap: bigint = 0n): bigint {
  if (after < before) throw new Error("balance decreased during acquisition");
  if (preexistingCap < 0n) throw new Error("preexisting cap cannot be negative");
  const allowedPreexisting = before < preexistingCap ? before : preexistingCap;
  return after - before + allowedPreexisting;
}

export function boundedBudgetSplit(total: bigint, tokenSidePpm: bigint): { swap: bigint; retain: bigint } {
  if (total <= 0n) throw new Error("budget must be positive");
  if (tokenSidePpm <= 0n || tokenSidePpm >= 1_000_000n) throw new Error("invalid token-side fraction");
  const swap = total * tokenSidePpm / 1_000_000n;
  if (swap <= 0n || swap >= total) throw new Error("budget too small to fund both sides");
  return { swap, retain: total - swap };
}

export function parseBoundedRaw(raw: string, capRaw: string): bigint {
  if (!/^[1-9][0-9]*$/.test(raw)) throw new Error("USDG_RAW must be a positive base-10 integer");
  if (!/^[1-9][0-9]*$/.test(capRaw)) throw new Error("RH_V4_MAX_USDG_RAW must be a positive base-10 integer");
  const amount = BigInt(raw), cap = BigInt(capRaw);
  if (amount > cap) throw new Error(`V4 USDG amount exceeds raw cap ${cap}`);
  return amount;
}

export interface SingleSidedRange {
  tickLower: number;
  tickUpper: number;
  usdgIsCurrency0: boolean;
}

/** Return an aligned range strictly on the USDG-only side of the current tick. */
export function singleSidedUsdgRange(
  currentTick: number,
  tickSpacing: number,
  usdgIsCurrency0: boolean,
  widthSpacings = 4,
): SingleSidedRange {
  if (!Number.isInteger(currentTick) || !Number.isInteger(tickSpacing) || tickSpacing <= 0)
    throw new Error("invalid pool tick or tickSpacing");
  if (!Number.isInteger(widthSpacings) || widthSpacings <= 0) throw new Error("range width must be positive");
  const floor = Math.floor(currentTick / tickSpacing) * tickSpacing;
  let tickLower: number;
  let tickUpper: number;
  if (usdgIsCurrency0) {
    tickLower = floor + tickSpacing;
    tickUpper = tickLower + widthSpacings * tickSpacing;
  } else {
    tickUpper = floor - (currentTick === floor ? tickSpacing : 0);
    tickLower = tickUpper - widthSpacings * tickSpacing;
  }
  if (tickLower < -887272 || tickUpper > 887272 || tickLower >= tickUpper)
    throw new Error("single-sided range exceeds usable tick bounds");
  if (!(usdgIsCurrency0 ? tickLower > currentTick : tickUpper < currentTick))
    throw new Error("single-sided range is not strictly outside current tick");
  return { tickLower, tickUpper, usdgIsCurrency0 };
}

/** Floor an expected receipt by slippage. A legitimately zero expected side stays zero;
 * callers must require at least one non-zero side so out-of-range single-sided positions remain closable. */
export function closeMinimum(expected: bigint, slippagePct: number): bigint {
  if (expected < 0n) throw new Error("expected amount cannot be negative");
  if (!Number.isFinite(slippagePct) || slippagePct < 0 || slippagePct >= 100)
    throw new Error("close slippage must be >= 0 and < 100 percent");
  if (expected === 0n) return 0n;
  const ppm = BigInt(Math.round(slippagePct * 10_000));
  const min = (expected * (1_000_000n - ppm)) / 1_000_000n;
  if (min <= 0n) throw new Error("cannot safely close: slippage produced zero minimum");
  return min;
}
