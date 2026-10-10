import type { Metadata } from "next";
import { defaultLocale, isAppLocale } from "@/shared/i18n/config";
import { AppLocaleProvider } from "@/shared/i18n/locale-provider";
import { getMessages } from "@/shared/i18n/messages";

import "./globals.css";
import "./workspace.css";
import { AppProviders } from "./providers";

const configuredLocale = process.env.NEXT_PUBLIC_DEFAULT_LOCALE;
const locale = isAppLocale(configuredLocale) ? configuredLocale : defaultLocale;
const localeMessages = getMessages(locale);

export const metadata: Metadata = {
  title: localeMessages.metadata.title,
  description: localeMessages.metadata.description,
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang={locale}>
      <body>
        <AppLocaleProvider initialLocale={locale}>
          <AppProviders>{children}</AppProviders>
        </AppLocaleProvider>
      </body>
    </html>
  );
}
