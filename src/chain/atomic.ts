import { ethers } from "ethers";

export type AtomicVersion = "v3" | "v4";
export interface AtomicBackendConfig { executor?: string; v4Executor?: string; atomicOnly: boolean }

/** Fail-closed adapter: callers submit the single encoded executor transaction themselves. */
export class AtomicBackend {
  constructor(readonly cfg: AtomicBackendConfig) {}
  requireAtomic(version: AtomicVersion): string {
    if (!this.cfg.atomicOnly) throw new Error("AtomicBackend requires ATOMIC_LP_ONLY=true");
    const candidate = version === "v4" ? this.cfg.v4Executor : this.cfg.executor;
    if (!candidate || !ethers.isAddress(candidate)) throw new Error(`${version === "v4" ? "ATOMIC_V4_EXECUTOR_ADDRESS" : "ATOMIC_EXECUTOR_ADDRESS"} missing/invalid`);
    return ethers.getAddress(candidate);
  }
  lifecycleTx(version: AtomicVersion, data: string): { to: string; data: string; value: bigint } {
    const to = this.requireAtomic(version);
    if (!ethers.isHexString(data) || ethers.dataLength(data) < 4) throw new Error("invalid executor calldata");
    return { to, data, value: 0n };
  }
}

export const atomicOnlyEnabled = (): boolean => /^(1|true|yes)$/i.test(process.env.ATOMIC_LP_ONLY ?? "");
export function refuseLegacyLifecycle(operation: string): never {
  throw new Error(`ATOMIC_LP_ONLY: refusing legacy multi-transaction ${operation}`);
}
