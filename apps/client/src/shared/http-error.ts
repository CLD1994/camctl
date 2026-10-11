import type { Issue } from "./types";
export class HttpError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly issues: Issue[] = [],
    readonly status = 0,
  ) {
    super(message);
  }
}
