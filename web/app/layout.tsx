import "./styles.css";

export const metadata = {
  title: "FinanceSEMA",
  description: "Evidence pujcek, majetku a investic",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="cs">
      <body>{children}</body>
    </html>
  );
}
