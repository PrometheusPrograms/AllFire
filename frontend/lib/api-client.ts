/**
 * Thin wrapper around the backend API base URL.
 * No request logic yet — see docs/ARCHITECTURE.md §3.
 */

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
