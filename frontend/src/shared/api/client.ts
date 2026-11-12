import type { ZodType } from "zod";

import { admissionRetryDetailsSchema, errorResponseSchema } from "./contracts";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly requestId?: string,
    readonly messageKey?: string,
    readonly fieldErrors: Array<{ field: string; code: string; messageKey: string }> = [],
    readonly details: Record<string, unknown> = {},
  ) {
    super(code);
    this.name = "ApiError";
  }
}

export function isDefinitiveMutationRejection(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  if (error.code === "INVALID_RESPONSE" || error.code === "UNEXPECTED_RESPONSE") return false;
  return error.status >= 400 && error.status < 500 && ![408, 425, 429].includes(error.status);
}

export function admissionErrorMessage(
  error: unknown,
  translate: (key: "rateLimited" | "aiAdmissionUnavailable", values: { seconds: number }) => string,
): string | null {
  if (!(error instanceof ApiError)) return null;
  const limited = error.status === 429 && error.code === "RATE_LIMITED";
  const unavailable = error.status === 503 && error.code === "AI_ADMISSION_UNAVAILABLE";
  if (!limited && !unavailable) return null;
  const parsed = admissionRetryDetailsSchema.safeParse(error.details);
  const seconds = parsed.success ? parsed.data.retry_after_seconds : limited ? 60 : 5;
  return translate(limited ? "rateLimited" : "aiAdmissionUnavailable", { seconds });
}

type RequestOptions<T> = {
  init?: RequestInit;
  schema: ZodType<T>;
  expectedStatus?: number;
};

export type ApiResult<T> = {
  data: T;
  etag: string | null;
  replayed: boolean;
};

async function parseBody(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type");
  if (!contentType?.includes("application/json")) {
    return undefined;
  }
  return response.json();
}

export async function requestJson<T>(
  path: string,
  { init, schema, expectedStatus }: RequestOptions<T>,
): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "include",
    headers: {
      Accept: "application/json",
      ...init?.headers,
    },
  });
  const body = await parseBody(response);

  if (!response.ok) {
    const parsedError = errorResponseSchema.safeParse(body);
    if (parsedError.success) {
      throw new ApiError(
        response.status,
        parsedError.data.error.code,
        parsedError.data.error.request_id,
        parsedError.data.error.message_key,
        parsedError.data.error.field_errors.map((fieldError) => ({
          field: fieldError.field,
          code: fieldError.code,
          messageKey: fieldError.message_key,
        })),
        parsedError.data.error.details,
      );
    }
    throw new ApiError(response.status, "UNEXPECTED_RESPONSE");
  }

  if (expectedStatus !== undefined && response.status !== expectedStatus) {
    throw new ApiError(response.status, "INVALID_RESPONSE");
  }

  const parsedBody = schema.safeParse(body);
  if (!parsedBody.success) {
    throw new ApiError(response.status, "INVALID_RESPONSE");
  }
  return parsedBody.data;
}

export async function requestJsonWithMetadata<T>(
  path: string,
  options: RequestOptions<T>,
): Promise<ApiResult<T>> {
  const response = await fetch(path, {
    ...options.init,
    credentials: "include",
    headers: { Accept: "application/json", ...options.init?.headers },
  });
  const body = await parseBody(response);
  if (!response.ok) {
    const parsedError = errorResponseSchema.safeParse(body);
    if (parsedError.success) {
      throw new ApiError(
        response.status,
        parsedError.data.error.code,
        parsedError.data.error.request_id,
        parsedError.data.error.message_key,
        parsedError.data.error.field_errors.map((fieldError) => ({
          field: fieldError.field,
          code: fieldError.code,
          messageKey: fieldError.message_key,
        })),
        parsedError.data.error.details,
      );
    }
    throw new ApiError(response.status, "UNEXPECTED_RESPONSE");
  }
  if (options.expectedStatus !== undefined && response.status !== options.expectedStatus) {
    throw new ApiError(response.status, "INVALID_RESPONSE");
  }
  const parsedBody = options.schema.safeParse(body);
  if (!parsedBody.success) {
    throw new ApiError(response.status, "INVALID_RESPONSE");
  }
  return {
    data: parsedBody.data,
    etag: response.headers.get("ETag"),
    replayed: response.headers.get("Idempotency-Replayed") === "true",
  };
}

export async function requestNoContent(path: string, init?: RequestInit): Promise<void> {
  const response = await fetch(path, {
    ...init,
    credentials: "include",
    headers: {
      Accept: "application/json",
      ...init?.headers,
    },
  });
  if (response.ok) {
    return;
  }

  const body = await parseBody(response);
  const parsedError = errorResponseSchema.safeParse(body);
  if (parsedError.success) {
    throw new ApiError(
      response.status,
      parsedError.data.error.code,
      parsedError.data.error.request_id,
      parsedError.data.error.message_key,
      parsedError.data.error.field_errors.map((fieldError) => ({
        field: fieldError.field,
        code: fieldError.code,
        messageKey: fieldError.message_key,
      })),
      parsedError.data.error.details,
    );
  }
  throw new ApiError(response.status, "UNEXPECTED_RESPONSE");
}
