import v3sdk from "@uniswap/v3-sdk";
const { Position } = v3sdk as any;

/** Expected token utilization for a V3 mint; unused desired-token dust is excluded. */
export function expectedMintAmounts(
  postSwapPool: any,
  tickLower: number,
  tickUpper: number,
  amount0Desired: bigint,
  amount1Desired: bigint,
) {
  const position = Position.fromAmounts({
    pool: postSwapPool,
    tickLower,
    tickUpper,
    amount0: amount0Desired.toString(),
    amount1: amount1Desired.toString(),
    useFullPrecision: true,
  });
  const mint = position.mintAmounts;
  return {
    amount0: BigInt(mint.amount0.toString()),
    amount1: BigInt(mint.amount1.toString()),
  };
}
