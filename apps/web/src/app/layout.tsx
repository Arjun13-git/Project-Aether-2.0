import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Aether — EO workbench",
  description: "Evidence-first Earth-observation snapshot and two-period change analysis",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="h-full bg-white font-sans">{children}</body>
    </html>
  );
}
