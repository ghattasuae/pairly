// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { AuthCard } from "./auth-card";
import { SignInForm } from "./sign-in-form";
import { TwoFactorVerify } from "./two-factor-verify";

const meta = {
  title: "Auth/SignIn",
  component: SignInForm,
  decorators: [
    (Story) => (
      <div className="bg-ledger flex min-h-[36rem] items-center justify-center p-8">
        <AuthCard title="Pairly" subtitle="">
          <Story />
        </AuthCard>
      </div>
    ),
  ],
  args: {
    sso: { google: false, microsoft: false },
    sendLink: async () => ({ ok: false, status: 429 }),
  },
} satisfies Meta<typeof SignInForm>;

export default meta;
type Story = StoryObj<typeof meta>;

export const MagicLink: Story = {};
export const MagicLinkArabic: Story = { globals: { locale: "ar" } };
export const WithSso: Story = { args: { sso: { google: true, microsoft: true } } };
export const WithSsoArabicDark: Story = { ...WithSso, globals: { locale: "ar", theme: "dark" } };

export const TwoFactor: Story = {
  render: () => <TwoFactorVerify verify={async () => ({ ok: false })} />,
};
export const TwoFactorArabicDark: Story = { ...TwoFactor, globals: { locale: "ar", theme: "dark" } };
