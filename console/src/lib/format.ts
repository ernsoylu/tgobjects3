/**
 * Small presentation-layer formatting helpers shared across console screens.
 */

const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"] as const;

/**
 * Render a byte count as a human-readable string using 1024-based units
 * (Requirement 19.1: stored bytes shown as human-readable B/KB/MB/GB/TB).
 *
 * Non-finite or negative inputs fall back to `0 B` so a bad value from the API
 * never renders as `NaN`. Sub-KB values stay integer; larger units show one
 * decimal place.
 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return "0 B";
  }
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < BYTE_UNITS.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  const unit = BYTE_UNITS[unitIndex] ?? "B";
  const rendered = unitIndex === 0 ? String(Math.round(value)) : value.toFixed(1);
  return `${rendered} ${unit}`;
}

/** Render an integer count with thousands separators for readability. */
export function formatCount(count: number): string {
  if (!Number.isFinite(count)) {
    return "0";
  }
  return Math.trunc(count).toLocaleString("en-US");
}
