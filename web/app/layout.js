import "./globals.css";

export const metadata = {
  title: "Oljan – kontrollpanel",
  description: "Brent/WTI intradagsbevakning: chart, nivåer, underrättelser och plan.",
};

export default function RootLayout({ children }) {
  return (
    <html lang="sv">
      <body>{children}</body>
    </html>
  );
}
