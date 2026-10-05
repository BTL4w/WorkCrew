"use client";

import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";

import { loginInputSchema } from "@/features/auth/api";
import { useAuth } from "@/features/auth/auth-provider";
import { ApiError } from "@/shared/api/client";
import { LocaleSwitcher } from "@/shared/i18n/locale-switcher";

type FieldErrors = {
  email?: string;
  password?: string;
};

export default function LoginPage() {
  const t = useTranslations("auth.login");
  const common = useTranslations("common");
  const router = useRouter();
  const {
    actor,
    reason,
    isBootstrapping,
    bootstrapError,
    isLoggingIn,
    login,
    retrySession,
  } = useAuth();
  const [email, setEmail] = useState("manager@example.test");
  const [password, setPassword] = useState("");
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [formError, setFormError] = useState<string | null>(null);

  useEffect(() => {
    if (!isBootstrapping && actor !== null) {
      router.replace("/");
    }
  }, [actor, isBootstrapping, router]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFieldErrors({});
    setFormError(null);

    const parsed = loginInputSchema.safeParse({ email: email.trim(), password });
    if (!parsed.success) {
      const flattened = parsed.error.flatten().fieldErrors;
      setFieldErrors({
        email: flattened.email ? t("validation.email") : undefined,
        password: flattened.password ? t("validation.password") : undefined,
      });
      return;
    }

    try {
      await login(parsed.data);
      router.replace("/");
    } catch (error) {
      if (error instanceof ApiError && error.code === "INVALID_CREDENTIALS") {
        setFormError(t("invalidCredentials"));
        return;
      }
      setFormError(t("unexpectedError"));
    }
  }

  if (isBootstrapping) {
    return (
      <main className="flex min-h-screen items-center justify-center px-6">
        <p role="status">{common("loadingSession")}</p>
      </main>
    );
  }

  return (
    <main className="grid min-h-screen bg-[var(--surface)] lg:grid-cols-[1.05fr_0.95fr]">
      <div className="absolute top-5 right-5 z-10"><LocaleSwitcher /></div>
      <section className="hidden bg-slate-950 p-12 text-white lg:flex lg:flex-col lg:justify-between">
        <div>
          <p className="text-sm font-semibold tracking-[0.22em] text-blue-300 uppercase">
            {t("product")}
          </p>
          <h1 className="hero-title mt-8 max-w-xl text-5xl leading-tight font-semibold">{t("heroTitle")}</h1>
          <p className="mt-6 max-w-lg text-lg leading-8 text-slate-300">{t("heroDescription")}</p>
        </div>
        <p className="max-w-md text-sm leading-6 text-slate-400">{t("localOnly")}</p>
      </section>

      <section className="flex items-center justify-center px-6 py-12 sm:px-12">
        <div className="w-full max-w-md">
          <p className="text-sm font-semibold tracking-wide text-[var(--accent)] uppercase">
            {t("eyebrow")}
          </p>
          <h2 className="section-title mt-3 text-3xl font-semibold">{t("title")}</h2>
          <p className="mt-3 leading-7 text-slate-600">{t("description")}</p>

          {reason === "SESSION_EXPIRED" ? (
            <p className="mt-6 rounded-xl bg-amber-50 p-4 text-sm text-amber-900" role="alert">
              {t("sessionExpired")}
            </p>
          ) : null}

          {bootstrapError ? (
            <div className="mt-6 rounded-xl bg-red-50 p-4 text-sm text-red-900" role="alert">
              <p>{common("sessionUnavailableDescription")}</p>
              <button className="mt-2 font-semibold underline" type="button" onClick={() => retrySession()}>
                {common("retry")}
              </button>
            </div>
          ) : null}

          <form className="mt-8 space-y-5" noValidate onSubmit={handleSubmit}>
            <div>
              <label className="text-sm font-medium" htmlFor="email">
                {t("email")}
              </label>
              <input
                aria-describedby={fieldErrors.email ? "email-error" : undefined}
                aria-invalid={Boolean(fieldErrors.email)}
                autoComplete="email"
                className="form-input mt-2"
                id="email"
                name="email"
                type="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
              />
              {fieldErrors.email ? (
                <p className="mt-2 text-sm text-red-700" id="email-error">
                  {fieldErrors.email}
                </p>
              ) : null}
            </div>

            <div>
              <label className="text-sm font-medium" htmlFor="password">
                {t("password")}
              </label>
              <input
                aria-describedby={fieldErrors.password ? "password-error" : undefined}
                aria-invalid={Boolean(fieldErrors.password)}
                autoComplete="current-password"
                className="form-input mt-2"
                id="password"
                name="password"
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
              {fieldErrors.password ? (
                <p className="mt-2 text-sm text-red-700" id="password-error">
                  {fieldErrors.password}
                </p>
              ) : null}
            </div>

            {formError ? (
              <p className="rounded-xl bg-red-50 p-4 text-sm text-red-800" role="alert">
                {formError}
              </p>
            ) : null}

            <button className="primary-button w-full" disabled={isLoggingIn} type="submit">
              {isLoggingIn ? t("submitting") : t("submit")}
            </button>
          </form>

          <div className="mt-8 rounded-2xl border border-[var(--border)] bg-slate-50 p-5">
            <p className="text-sm font-semibold">{t("demoTitle")}</p>
            <p className="mt-2 text-sm leading-6 text-slate-600">{t("demoDescription")}</p>
            <p className="mt-3 font-mono text-sm text-slate-700">admin@example.test</p>
            <p className="font-mono text-sm text-slate-700">manager@example.test</p>
            <p className="font-mono text-sm text-slate-700">employee@example.test</p>
          </div>
        </div>
      </section>
    </main>
  );
}
